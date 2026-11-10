"""Rebuildable object full-text projections and their durable work checkpoints.

These tables are derived data, not immutable evidence. A full-text checkpoint
does not assert that vector or document projections have completed.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord, now


def project_scope(name):
    return ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name=name)


def entity_scope(name):
    return ForeignKeyConstraint(
        ["tenant_id", "project_id", "entity_id"],
        ["entities.tenant_id", "entities.project_id", "entities.id"],
        name=name,
    )


def release_scope(name):
    return ForeignKeyConstraint(
        ["tenant_id", "project_id", "release_id"],
        ["releases.tenant_id", "releases.project_id", "releases.id"],
        name=name,
    )


class ObjectSearchDocument(ProjectRecord, Base):
    __tablename__ = "object_search_documents"
    __table_args__ = (
        UniqueConstraint("entity_id", name="uq_search_document_entity"),
        project_scope("fk_search_document_project_scope"),
        entity_scope("fk_search_document_entity_scope"),
        release_scope("fk_search_document_release_scope"),
        CheckConstraint("entity_revision >= 1", name="ck_search_document_revision"),
        CheckConstraint("status IN ('ready', 'removed', 'error')", name="ck_search_document_status"),
    )
    entity_id: Mapped[str] = mapped_column(String(36))
    entity_revision: Mapped[int] = mapped_column(Integer)
    release_id: Mapped[str] = mapped_column(String(36))
    generation: Mapped[str] = mapped_column(String(36))
    tokenizer_version: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_at: Mapped[str] = mapped_column(String(40), default=now)


class ObjectSearchField(ProjectRecord, Base):
    __tablename__ = "object_search_fields"
    __table_args__ = (
        UniqueConstraint("entity_id", "field_key", "chunk", name="uq_search_field_chunk"),
        project_scope("fk_search_field_project_scope"),
        entity_scope("fk_search_field_entity_scope"),
        CheckConstraint("chunk >= 0", name="ck_search_field_chunk"),
        Index(
            "ix_object_search_fields_fts",
            text("to_tsvector('simple'::regconfig, token_text)"),
            postgresql_using="gin",
        ).ddl_if(dialect="postgresql"),
    )
    entity_id: Mapped[str] = mapped_column(String(36))
    field_key: Mapped[str] = mapped_column(String(120))
    chunk: Mapped[int] = mapped_column(Integer)
    token_text: Mapped[str] = mapped_column(Text)
    generation: Mapped[str] = mapped_column(String(36))


class ObjectSearchRequest(ProjectRecord, Base):
    __tablename__ = "object_search_requests"
    __table_args__ = (
        UniqueConstraint("entity_id", name="uq_search_request_entity"),
        project_scope("fk_search_request_project_scope"),
        entity_scope("fk_search_request_entity_scope"),
        CheckConstraint("requested_revision >= 1", name="ck_search_request_revision"),
    )
    entity_id: Mapped[str] = mapped_column(String(36))
    requested_revision: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class SearchProjectState(ProjectRecord, Base):
    __tablename__ = "search_project_states"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_search_state_project"),
        project_scope("fk_search_state_project_scope"),
        release_scope("fk_search_state_release_scope"),
    )
    release_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    generation: Mapped[str] = mapped_column(String(36))
    tokenizer_version: Mapped[str] = mapped_column(String(120))
    backfill_cursor: Mapped[str] = mapped_column(String(36), default="", server_default="")
    backfill_complete: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class SearchCdcCheckpoint(ProjectRecord, Base):
    __tablename__ = "search_cdc_checkpoints"
    __table_args__ = (
        UniqueConstraint("stream_id", name="uq_search_checkpoint_stream"),
        project_scope("fk_search_checkpoint_project_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "stream_id"],
            ["cdc_streams.tenant_id", "cdc_streams.project_id", "cdc_streams.id"],
            name="fk_search_checkpoint_stream_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "stream_id", "binding_id"],
            [
                "cdc_projection_bindings.tenant_id",
                "cdc_projection_bindings.project_id",
                "cdc_projection_bindings.stream_id",
                "cdc_projection_bindings.id",
            ],
            name="fk_search_checkpoint_binding_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "stream_id", "binding_id", "window_id"],
            [
                "cdc_projection_windows.tenant_id",
                "cdc_projection_windows.project_id",
                "cdc_projection_windows.stream_id",
                "cdc_projection_windows.binding_id",
                "cdc_projection_windows.id",
            ],
            name="fk_search_checkpoint_window_scope",
        ),
        CheckConstraint("sequence >= 0", name="ck_search_checkpoint_sequence"),
    )
    stream_id: Mapped[str] = mapped_column(String(36))
    binding_id: Mapped[str] = mapped_column(String(36))
    generation: Mapped[str] = mapped_column(String(36))
    sequence: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    window_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
