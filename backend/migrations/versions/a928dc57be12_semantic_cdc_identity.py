"""Deduplicate native events by semantic identity, preserving original records."""

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "a928dc57be12"
down_revision = "783bb791c5d2"
branch_labels = None
depends_on = None


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def stable_record(record):
    # Frozen migration logic: never import mutable application normalizers here.
    result = {
        key: value
        for key, value in record.items()
        if key not in {"timestamp", "event_id", "identity_material"}
    }
    result["source_offset"] = {
        key: value for key, value in record["source_offset"].items() if key not in {"lsn_proc", "lsn_commit"}
    }
    if record["event_kind"] in {"data", "heartbeat"} and isinstance(record["value"]["payload"], dict):
        result["value"] = {
            **record["value"],
            "payload": {
                key: value
                for key, value in record["value"]["payload"].items()
                if key not in {"ts_ms", "ts_us", "ts_ns"}
            },
        }
    return result


def upgrade():
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    if postgres:
        op.execute("LOCK TABLE cdc_events IN ACCESS EXCLUSIVE MODE")
    with op.batch_alter_table("cdc_events") as batch:
        batch.add_column(sa.Column("identity_hash", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_cdc_semantic_identity", ["stream_id", "identity_hash"])
    events = sa.table(
        "cdc_events",
        sa.column("id", sa.String()),
        sa.column("tenant_id", sa.String()),
        sa.column("record", sa.JSON()),
        sa.column("identity_hash", sa.String()),
        sa.column("record_hash", sa.String()),
    )
    # Only derived metadata is updated. The immutable wire record, sequence and
    # first event_id are retained. PostgreSQL DDL/trigger changes are transactional.
    if postgres:
        op.execute("ALTER TABLE cdc_events DISABLE TRIGGER immutable_history")
    for tenant_id in bind.execute(sa.text("SELECT id FROM tenants")).scalars().all():
        if postgres:
            bind.execute(sa.text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        cursor = ""
        while True:
            rows = bind.execute(
                sa.select(events.c.id, events.c.record)
                .where(events.c.tenant_id == tenant_id, events.c.id > cursor)
                .order_by(events.c.id)
                .limit(500)
            ).all()
            if not rows:
                break
            for row in rows:
                try:
                    bind.execute(
                        events.update()
                        .where(events.c.id == row.id)
                        .values(
                            identity_hash=digest(json.loads(row.record["identity_material"])),
                            record_hash=digest(stable_record(row.record)),
                        )
                    )
                except sa.exc.IntegrityError as exc:
                    raise RuntimeError(
                        "Existing CDC events share a semantic identity; review the retained events before migrating. No event was silently removed."
                    ) from exc
            cursor = rows[-1].id
    if postgres:
        op.execute("ALTER TABLE cdc_events ENABLE TRIGGER immutable_history")
        bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
    with op.batch_alter_table("cdc_events") as batch:
        batch.alter_column("identity_hash", existing_type=sa.String(64), nullable=False)


def downgrade():
    raise RuntimeError(
        "CDC identity changes require a reviewed migration; automatic downgrade is unsupported"
    )
