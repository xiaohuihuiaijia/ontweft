"""At-least-once native event receipt with commit-before-ACK semantics."""

import hashlib
import hmac
import json
import secrets
import time
from contextlib import contextmanager
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from ontology.cdc.models import CdcEvent, CdcStream
from ontology.core.errors import DomainError, conflict
from ontology.identity.models import Project, User
from ontology.identity.security import access, token_hash
from ontology.sources.ingestion import fingerprint
from ontology.sources.models import Source

LEASE_SECONDS = 60
MAX_BATCH_BYTES = 8 * 1024 * 1024


class WireModel(BaseModel):
    # Whitespace in raw keys and source values is significant.
    model_config = ConfigDict(extra="forbid", strict=True)


class ConnectValue(WireModel):
    schema_: dict[str, Any] | None = Field(alias="schema")
    payload: Any


class ConnectHeader(WireModel):
    key: str
    schema_: dict[str, Any] | None = Field(alias="schema")
    value: Any


class NativeEvent(WireModel):
    event_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity_material: str = Field(max_length=2_000_000)
    event_kind: Literal["data", "tombstone", "transaction", "heartbeat", "schema", "control"]
    connector: Literal["postgresql"]
    topic: str = Field(min_length=1, max_length=1000)
    kafka_partition: int | None
    timestamp: int | None
    source_partition: dict[str, Any]
    source_offset: dict[str, Any]
    key: ConnectValue
    value: ConnectValue
    headers: list[ConnectHeader] = Field(max_length=1000)
    operation: str | None = Field(max_length=30)
    transaction: dict[str, Any] | None
    snapshot: str | bool | None


class NativeBatch(WireModel):
    holder_id: str = Field(min_length=36, max_length=36)
    fencing_token: int = Field(ge=1)
    generation: str = Field(min_length=36, max_length=36)
    events: list[NativeEvent] = Field(min_length=1, max_length=1000)


def reject_constant(_):
    raise ValueError("Non-finite JSON numbers are unsupported")


def strict_json(value):
    return json.loads(value, parse_constant=reject_constant)


async def read_batch(request):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BATCH_BYTES:
            raise DomainError(413, "cdc_batch_budget", "CDC 批次超过 8 MiB，须分批持久暂存")
        data.extend(chunk)
    try:
        return NativeBatch.model_validate(strict_json(data))
    except (ValueError, ValidationError, RecursionError) as exc:
        raise DomainError(422, "invalid_cdc_envelope", "CDC 事件信封格式不合法") from exc


def issue_token(row):
    token = f"cdc.{row.tenant_id}.{row.id}.{secrets.token_urlsafe(32)}"
    row.token_hash = token_hash(token)
    return token


def token_scope(authorization, stream_id):
    try:
        scheme, token = authorization.split(" ", 1)
        prefix, tenant_id, embedded_id, secret = token.split(".")
        if scheme != "Bearer" or prefix != "cdc" or embedded_id != stream_id or len(secret) < 40:
            raise ValueError()
        if str(UUID(tenant_id)) != tenant_id or str(UUID(embedded_id)) != embedded_id:
            raise ValueError()
    except (AttributeError, ValueError) as exc:
        raise DomainError(401, "cdc_unauthenticated", "需要该来源专属的采集凭据") from exc
    return tenant_id, token


@contextmanager
def authenticated_stream(db, authorization, stream_id):
    tenant_id, token = token_scope(authorization, stream_id)
    with db.transaction(tenant_id) as session:
        row = session.scalar(
            select(CdcStream).where(CdcStream.id == stream_id, CdcStream.tenant_id == tenant_id)
        )
        if not row or not hmac.compare_digest(row.token_hash, token_hash(token)):
            raise DomainError(401, "cdc_unauthenticated", "采集凭据无效")
        # Shared project lock coordinates source, membership and pause changes;
        # stream row lock orders receipt without serializing unrelated streams.
        session.scalar(select(Project).where(Project.id == row.project_id).with_for_update(read=True))
        row = session.scalar(
            select(CdcStream)
            .where(CdcStream.id == stream_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not hmac.compare_digest(row.token_hash, token_hash(token)):
            raise DomainError(401, "cdc_unauthenticated", "采集凭据已轮换")
        owner = session.scalar(
            select(User)
            .where(User.id == row.owner_id, User.tenant_id == tenant_id)
            .with_for_update(read=True)
        )
        if not owner or not owner.active:
            raise DomainError(403, "cdc_authority_revoked", "采集授权已撤销")
        ctx = access(session, owner, row.project_id)
        if "sources:write" not in ctx.permissions:
            raise DomainError(403, "cdc_authority_revoked", "当前配置者不再拥有来源接入权限")
        source = session.get(Source, row.source_id)
        if row.state != "active":
            conflict("采集通道已暂停或等待重新建立基线")
        if source is None or source.revision != row.source_revision:
            conflict("来源配置已改变，须核对采集基线")
        yield session, row
    # The caller's ACK is constructed inside but returned only after this commit.


def validate_identity(event, generation):
    material = event.identity_material.encode("utf-8")
    if hashlib.sha256(material).hexdigest() != event.event_id:
        raise DomainError(422, "cdc_identity_invalid", "事件身份摘要不匹配")
    expected = {
        "identity_version": 1,
        "generation": generation,
        "source_partition": event.source_partition,
        "source_position": {
            key: event.source_offset[key]
            for key in ("lsn", "txId", "ts_usec", "messageType", "snapshot")
            if key in event.source_offset
        },
        "topic": event.topic,
        "event_kind": event.event_kind,
        "key_payload": event.key.payload,
        "transaction": event.transaction,
        "operation": event.operation,
        "snapshot": event.snapshot,
    }
    try:
        supplied = strict_json(material)
        if fingerprint(supplied) != fingerprint(expected):
            raise ValueError()
    except (ValueError, TypeError, RecursionError) as exc:
        raise DomainError(422, "cdc_identity_invalid", "事件身份与原生位点、键或事务边界不一致") from exc
    # JSON formatting belongs to the transport, not the source event identity.
    return fingerprint(expected)


def stable_record(record):
    result = {**record}
    result.pop("timestamp", None)
    result.pop("event_id", None)
    result.pop("identity_material", None)
    result["source_offset"] = {
        key: value for key, value in record["source_offset"].items() if key not in {"lsn_proc", "lsn_commit"}
    }
    if record["event_kind"] in {"data", "heartbeat"} and isinstance(record["value"]["payload"], dict):
        payload = {**record["value"]["payload"]}
        for field in ("ts_ms", "ts_us", "ts_ns"):
            payload.pop(field, None)
        result["value"] = {**record["value"], "payload": payload}
    return result


def receive_batch(session, stream, batch):
    current = time.time()
    if (
        stream.lease_holder != batch.holder_id
        or stream.fencing_token != batch.fencing_token
        or stream.lease_until <= current
    ):
        conflict("采集租约已失效，旧采集端不能确认或追加事件")
    if stream.generation != batch.generation:
        conflict("来源代次不匹配，普通重启不能新建代次")
    accepted, inserted = [], 0
    for event in batch.events:
        identity_hash = validate_identity(event, stream.generation)
        if event.connector != stream.connector:
            conflict("事件连接器与采集通道不匹配")
        if stream.source_partition is None:
            stream.source_partition = event.source_partition
        elif fingerprint(stream.source_partition) != fingerprint(event.source_partition):
            conflict("当前通道只接受固定来源分区，不能混入其他数据库")
        record = event.model_dump(mode="json", by_alias=True)
        checksum = fingerprint(stable_record(record))
        existing = session.scalar(
            select(CdcEvent).where(CdcEvent.stream_id == stream.id, CdcEvent.identity_hash == identity_hash)
        )
        if existing:
            if existing.record_hash != checksum:
                conflict("相同事件身份对应不同源内容，整批不予确认")
        else:
            stream.received_sequence += 1
            session.add(
                CdcEvent(
                    tenant_id=stream.tenant_id,
                    project_id=stream.project_id,
                    stream_id=stream.id,
                    sequence=stream.received_sequence,
                    event_id=event.event_id,
                    identity_hash=identity_hash,
                    record_hash=checksum,
                    record=record,
                )
            )
            session.flush()
            inserted += 1
        accepted.append(event.event_id)
    stream.lease_until = current + LEASE_SECONDS
    return {
        "accepted_event_ids": accepted,
        "inserted": inserted,
        "received_sequence": stream.received_sequence,
        "projected_sequence": stream.projected_sequence,
        "indexed_sequence": stream.indexed_sequence,
        "receipt_scope": "durable_inbox_only",
    }
