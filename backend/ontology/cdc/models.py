"""Durable source event inbox; receipt is separate from business projection."""

from sqlalchemy import (
    JSON,
    BigInteger,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord


class CdcStream(ProjectRecord, Base):
    __tablename__ = "cdc_streams"
    __table_args__ = (
        UniqueConstraint("source_id"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_cdc_stream_scope"),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "id",
            "source_id",
            "source_revision",
            "generation",
            name="uq_cdc_stream_binding_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_id"],
            ["sources.tenant_id", "sources.project_id", "sources.id"],
            name="fk_cdc_stream_source_scope",
        ),
    )
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    source_revision: Mapped[int] = mapped_column(Integer)
    generation: Mapped[str] = mapped_column(String(36))
    connector: Mapped[str] = mapped_column(String(30))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30), default="active")
    received_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    projected_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    indexed_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    source_partition: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    lease_holder: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)


class CdcEvent(ProjectRecord, Base):
    __tablename__ = "cdc_events"
    __table_args__ = (
        UniqueConstraint("stream_id", "event_id"),
        UniqueConstraint("stream_id", "identity_hash", name="uq_cdc_semantic_identity"),
        UniqueConstraint("stream_id", "sequence"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_cdc_event_scope"),
    )
    stream_id: Mapped[str] = mapped_column(ForeignKey("cdc_streams.id"), index=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    event_id: Mapped[str] = mapped_column(String(64))
    identity_hash: Mapped[str] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(String(64))
    record: Mapped[dict] = mapped_column(JSON)
