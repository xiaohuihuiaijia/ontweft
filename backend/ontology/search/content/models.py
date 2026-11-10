"""Rebuildable full-text content projections, separate from published evidence."""

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord, now


def project_scope(prefix):
    return ForeignKeyConstraint(
        ["tenant_id", "project_id"],
        ["projects.tenant_id", "projects.id"],
        name=f"fk_content_search_{prefix}_project_scope",
    )


def asset_constraints(prefix, *, revision=False):
    document_branch = (
        "asset_kind = 'document' AND document_id IS NOT NULL AND asset_id = document_id AND page_id IS NULL"
    )
    knowledge_branch = (
        "asset_kind = 'knowledge' AND page_id IS NOT NULL AND asset_id = page_id AND document_id IS NULL"
    )
    if revision:
        document_branch += " AND knowledge_revision_id IS NULL"
        knowledge_branch += " AND knowledge_revision_id IS NOT NULL"
    return (
        UniqueConstraint("asset_kind", "asset_id", name=f"uq_content_search_{prefix}_asset"),
        project_scope(prefix),
        CheckConstraint(
            f"({document_branch}) OR ({knowledge_branch})", name=f"ck_content_search_{prefix}_asset"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "document_id"],
            ["documents.tenant_id", "documents.project_id", "documents.id"],
            name=f"fk_content_search_{prefix}_document_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "page_id"],
            ["knowledge_pages.tenant_id", "knowledge_pages.project_id", "knowledge_pages.id"],
            name=f"fk_content_search_{prefix}_page_scope",
        ),
    )


class ContentSearchState(ProjectRecord, Base):
    __tablename__ = "content_search_states"
    __table_args__ = (
        UniqueConstraint("project_id", "kind", name="uq_content_search_state_project_kind"),
        project_scope("state"),
        CheckConstraint("kind IN ('document', 'knowledge')", name="ck_content_search_state_kind"),
    )
    kind: Mapped[str] = mapped_column(String(20))
    generation: Mapped[str] = mapped_column(String(36))
    tokenizer_version: Mapped[str] = mapped_column(String(120))
    chunker_version: Mapped[str] = mapped_column(String(120))
    backfill_cursor: Mapped[str] = mapped_column(String(36), default="", server_default="")
    backfill_complete: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sql_text("false"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class ContentSearchDocument(ProjectRecord, Base):
    __tablename__ = "content_search_documents"
    __table_args__ = (
        *asset_constraints("document", revision=True),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_content_search_document_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "page_id", "knowledge_revision_id"],
            [
                "knowledge_revisions.tenant_id",
                "knowledge_revisions.project_id",
                "knowledge_revisions.page_id",
                "knowledge_revisions.id",
            ],
            name="fk_content_search_document_revision_scope",
        ),
        CheckConstraint("source_revision >= 1", name="ck_content_search_document_revision"),
        CheckConstraint("parse_revision >= 0", name="ck_content_search_document_parse_revision"),
        CheckConstraint(
            "status IN ('ready', 'partial', 'removed', 'error')", name="ck_content_search_document_status"
        ),
    )
    asset_kind: Mapped[str] = mapped_column(String(20))
    asset_id: Mapped[str] = mapped_column(String(36))
    document_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    page_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    knowledge_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_revision: Mapped[int] = mapped_column(Integer)
    parse_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(250))
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    generation: Mapped[str] = mapped_column(String(36))
    tokenizer_version: Mapped[str] = mapped_column(String(120))
    chunker_version: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_at: Mapped[str] = mapped_column(String(40), default=now)


class ContentSearchChunk(ProjectRecord, Base):
    __tablename__ = "content_search_chunks"
    __table_args__ = (
        UniqueConstraint("receipt_id", "ordinal", name="uq_content_search_chunk_ordinal"),
        project_scope("chunk"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "receipt_id"],
            [
                "content_search_documents.tenant_id",
                "content_search_documents.project_id",
                "content_search_documents.id",
            ],
            name="fk_content_search_chunk_receipt_scope",
        ),
        CheckConstraint("ordinal >= 0", name="ck_content_search_chunk_ordinal"),
        Index(
            "ix_content_search_chunks_fts",
            sql_text("to_tsvector('simple'::regconfig, token_text)"),
            postgresql_using="gin",
        ).ddl_if(dialect="postgresql"),
    )
    receipt_id: Mapped[str] = mapped_column(String(36))
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_text: Mapped[str] = mapped_column(Text)
    anchor: Mapped[dict] = mapped_column(JSON)


class ContentSearchRequest(ProjectRecord, Base):
    __tablename__ = "content_search_requests"
    __table_args__ = asset_constraints("request")
    asset_kind: Mapped[str] = mapped_column(String(20))
    asset_id: Mapped[str] = mapped_column(String(36))
    document_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    page_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
