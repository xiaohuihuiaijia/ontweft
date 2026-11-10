"""Immutable provenance and versioned employee authorization observations."""

from sqlalchemy import (
    DDL,
    CheckConstraint,
    Float,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord


class DocumentSource(ProjectRecord, Base):
    __tablename__ = "document_sources"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_document_source_document"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_document_source_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "document_id"],
            ["documents.tenant_id", "documents.project_id", "documents.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"], ["feishu_applications.tenant_id", "feishu_applications.id"]
        ),
        CheckConstraint("service IN ('feishu', 'lark')", name="ck_document_source_service"),
    )
    document_id: Mapped[str] = mapped_column(String(36))
    application_id: Mapped[str] = mapped_column(String(36))
    service: Mapped[str] = mapped_column(String(16))
    external_tenant_key: Mapped[str] = mapped_column(String(200))
    resource_type: Mapped[str] = mapped_column(String(30))
    resource_token: Mapped[str] = mapped_column(String(512))


class KnowledgeDependency(ProjectRecord, Base):
    __tablename__ = "knowledge_source_dependencies"
    __table_args__ = (
        UniqueConstraint("page_id", "revision", "document_id", name="uq_knowledge_dependency"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "page_id"],
            ["knowledge_pages.tenant_id", "knowledge_pages.project_id", "knowledge_pages.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "document_id"],
            ["documents.tenant_id", "documents.project_id", "documents.id"],
        ),
        CheckConstraint("revision >= 1", name="ck_knowledge_dependency_revision"),
    )
    page_id: Mapped[str] = mapped_column(String(36), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    document_id: Mapped[str] = mapped_column(String(36))


class SourcePermission(ProjectRecord, Base):
    __tablename__ = "content_source_permissions"
    __table_args__ = (
        UniqueConstraint("source_id", "user_id", "action", name="uq_content_source_permission"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_id"],
            ["document_sources.tenant_id", "document_sources.project_id", "document_sources.id"],
        ),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        CheckConstraint("action IN ('view', 'export')", name="ck_content_permission_action"),
        CheckConstraint(
            "status IN ('checking', 'allowed', 'denied', 'unknown')", name="ck_content_permission_status"
        ),
        CheckConstraint("revision >= 1", name="ck_content_permission_revision"),
    )
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(20))
    account_revision: Mapped[int] = mapped_column(Integer)
    user_revision: Mapped[int] = mapped_column(Integer)
    application_revision: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="checking")
    checked_at: Mapped[float] = mapped_column(Float, default=0)
    valid_until: Mapped[float] = mapped_column(Float, default=0)
    lease_until: Mapped[float] = mapped_column(Float, default=0)


for immutable_model in (DocumentSource, KnowledgeDependency):
    for operation in ("UPDATE", "DELETE"):
        table = immutable_model.__tablename__
        event.listen(
            immutable_model.__table__,
            "after_create",
            DDL(
                f"CREATE TRIGGER immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'Source provenance is immutable'); END"
            ).execute_if(dialect="sqlite"),
        )
