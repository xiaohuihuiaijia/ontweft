"""Frozen projection bindings, mutable progress, and immutable committed windows."""

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord


def stream_scope(name):
    return ForeignKeyConstraint(
        ["tenant_id", "project_id", "stream_id", "source_id", "source_revision", "generation"],
        [
            "cdc_streams.tenant_id",
            "cdc_streams.project_id",
            "cdc_streams.id",
            "cdc_streams.source_id",
            "cdc_streams.source_revision",
            "cdc_streams.generation",
        ],
        name=name,
    )


def binding_scope(name):
    return ForeignKeyConstraint(
        ["tenant_id", "project_id", "stream_id", "binding_id"],
        [
            "cdc_projection_bindings.tenant_id",
            "cdc_projection_bindings.project_id",
            "cdc_projection_bindings.stream_id",
            "cdc_projection_bindings.id",
        ],
        name=name,
    )


class CdcProjectionBinding(ProjectRecord, Base):
    __tablename__ = "cdc_projection_bindings"
    __table_args__ = (
        UniqueConstraint("stream_id"),
        UniqueConstraint("tenant_id", "project_id", "stream_id", "id", name="uq_cdc_binding_scope"),
        stream_scope("fk_cdc_binding_stream_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "release_id"],
            ["releases.tenant_id", "releases.project_id", "releases.id"],
            name="fk_cdc_binding_release_scope",
        ),
        CheckConstraint("source_revision >= 1", name="ck_cdc_binding_revision"),
    )
    stream_id: Mapped[str] = mapped_column(ForeignKey("cdc_streams.id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    source_revision: Mapped[int] = mapped_column(Integer)
    generation: Mapped[str] = mapped_column(String(36))
    release_id: Mapped[str] = mapped_column(ForeignKey("releases.id"))
    resources: Mapped[list] = mapped_column(JSON)
    binding_hash: Mapped[str] = mapped_column(String(64))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))


class CdcProjectionState(ProjectRecord, Base):
    __tablename__ = "cdc_projection_states"
    __table_args__ = (
        UniqueConstraint("stream_id"),
        UniqueConstraint("binding_id"),
        binding_scope("fk_cdc_state_binding_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "stream_id", "binding_id", "baseline_window_id"],
            [
                "cdc_projection_windows.tenant_id",
                "cdc_projection_windows.project_id",
                "cdc_projection_windows.stream_id",
                "cdc_projection_windows.binding_id",
                "cdc_projection_windows.id",
            ],
            name="fk_cdc_state_baseline_scope",
        ),
        CheckConstraint("observed_sequence >= 0", name="ck_cdc_state_sequence"),
    )
    stream_id: Mapped[str] = mapped_column(ForeignKey("cdc_streams.id"))
    binding_id: Mapped[str] = mapped_column(ForeignKey("cdc_projection_bindings.id"))
    status: Mapped[str] = mapped_column(String(30), default="waiting")
    reason_code: Mapped[str] = mapped_column(String(100), default="inbox_empty")
    message: Mapped[str] = mapped_column(Text, default="等待原生初始快照及完整结束证明")
    observed_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    schema_fingerprints: Mapped[dict] = mapped_column(JSON, default=dict)
    baseline_window_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)


class CdcProjectionWindow(ProjectRecord, Base):
    __tablename__ = "cdc_projection_windows"
    __table_args__ = (
        UniqueConstraint("stream_id", "end_sequence"),
        UniqueConstraint(
            "tenant_id", "project_id", "stream_id", "binding_id", "id", name="uq_cdc_window_scope"
        ),
        binding_scope("fk_cdc_window_binding_scope"),
        CheckConstraint("start_sequence >= 1 AND end_sequence >= start_sequence", name="ck_cdc_window_range"),
    )
    binding_id: Mapped[str] = mapped_column(ForeignKey("cdc_projection_bindings.id"))
    stream_id: Mapped[str] = mapped_column(ForeignKey("cdc_streams.id"))
    start_sequence: Mapped[int] = mapped_column(BigInteger)
    end_sequence: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(30))
    proof: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    schema_fingerprints: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    # This durable list is the pending input for downstream indexes; no index
    # worker may infer success merely from the business projection cursor.
    entity_ids: Mapped[list] = mapped_column(JSON)
    source_position: Mapped[dict] = mapped_column(JSON)
