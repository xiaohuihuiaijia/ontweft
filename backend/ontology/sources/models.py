from sqlalchemy import (
    JSON,
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


class Source(ProjectRecord, Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("tenant_id", "project_id", "id", name="uq_source_scope"),)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(40))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    secret: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="unverified")
    resources: Mapped[list] = mapped_column(JSON, default=list)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SourceSnapshot(ProjectRecord, Base):
    __tablename__ = "source_snapshots"
    __table_args__ = (
        UniqueConstraint("source_id", "resource_id", "generation"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_snapshot_scope"),
    )
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    source_revision: Mapped[int] = mapped_column(Integer)
    resource_id: Mapped[str] = mapped_column(String(500))
    generation: Mapped[int] = mapped_column(Integer, default=1)
    schema_fingerprint: Mapped[str] = mapped_column(String(64))
    release_id: Mapped[str | None] = mapped_column(ForeignKey("releases.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    consistency: Mapped[str] = mapped_column(String(30), default="unknown")
    started_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    projection: Mapped[dict] = mapped_column(JSON, default=dict)


class SourceRecordRevision(ProjectRecord, Base):
    __tablename__ = "source_record_revisions"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "attempt", "key_hash"),
        UniqueConstraint("cdc_event_id", name="uq_source_record_cdc_event"),
        UniqueConstraint(
            "tenant_id", "project_id", "id", "record_codec", name="uq_source_record_scope_codec"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "snapshot_id"],
            ["source_snapshots.tenant_id", "source_snapshots.project_id", "source_snapshots.id"],
            name="fk_source_record_snapshot_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "cdc_event_id"],
            ["cdc_events.tenant_id", "cdc_events.project_id", "cdc_events.id"],
            name="fk_source_record_cdc_scope",
        ),
        CheckConstraint(
            "(record_codec = 'snapshot-json-v1' AND snapshot_id IS NOT NULL AND attempt IS NOT NULL "
            "AND attempt >= 1 AND cdc_event_id IS NULL AND key_hash IS NOT NULL "
            'AND source_key IS NOT NULL AND "values" IS NOT NULL AND content_hash IS NOT NULL) OR '
            "(record_codec = 'cdc-native-reference-v1' AND cdc_event_id IS NOT NULL "
            "AND snapshot_id IS NULL AND attempt IS NULL AND key_hash IS NULL AND source_key IS NULL "
            'AND identity_key IS NULL AND "values" IS NULL AND content_hash IS NULL)',
            name="ck_source_record_exact_origin",
        ),
    )
    record_codec: Mapped[str] = mapped_column(
        String(40), default="snapshot-json-v1", server_default="snapshot-json-v1"
    )
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_snapshots.id"), index=True, nullable=True
    )
    cdc_event_id: Mapped[str | None] = mapped_column(ForeignKey("cdc_events.id"), nullable=True)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # CDC references intentionally carry no decoded values. In particular SQL
    # NULL must not be encoded as the JSON literal null by SQLAlchemy.
    source_key: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    identity_key: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    values: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SourceIdentity(ProjectRecord, Base):
    __tablename__ = "source_identities"
    __table_args__ = (
        UniqueConstraint("source_id", "resource_id", "key_hash", "type_key"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_source_identity_scope"),
        CheckConstraint(
            "last_record_codec IN ('snapshot-json-v1', 'cdc-native-reference-v1')",
            name="ck_identity_projectable_record",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "last_record_id", "last_record_codec"],
            [
                "source_record_revisions.tenant_id",
                "source_record_revisions.project_id",
                "source_record_revisions.id",
                "source_record_revisions.record_codec",
            ],
            name="fk_identity_record_scope_codec",
        ),
    )
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    resource_id: Mapped[str] = mapped_column(String(500))
    key_hash: Mapped[str] = mapped_column(String(64))
    key_codec: Mapped[str] = mapped_column(
        String(30), default="legacy-json-v1", server_default="legacy-json-v1"
    )
    type_key: Mapped[str] = mapped_column(String(120))
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    last_record_id: Mapped[str] = mapped_column(ForeignKey("source_record_revisions.id"))
    last_record_codec: Mapped[str] = mapped_column(
        String(40), default="snapshot-json-v1", server_default="snapshot-json-v1"
    )
    mapping_key: Mapped[str] = mapped_column(String(80))


class SourceFileVersion(ProjectRecord, Base):
    __tablename__ = "source_file_versions"
    __table_args__ = (
        UniqueConstraint("source_id", "number", name="uq_source_file_number"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_id"],
            ["sources.tenant_id", "sources.project_id", "sources.id"],
            name="fk_file_version_source",
        ),
    )
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    number: Mapped[int] = mapped_column(Integer)
    base_revision: Mapped[int] = mapped_column(Integer)
    applied_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config: Mapped[dict] = mapped_column(JSON)
    resources: Mapped[list] = mapped_column(JSON)
    preview: Mapped[dict] = mapped_column(JSON, default=dict)
    jobs: Mapped[list] = mapped_column(JSON, default=list)
