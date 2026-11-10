"""Append-only document parse evidence, independent of replaceable search rows."""

from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord


class DocumentParseVersion(ProjectRecord, Base):
    __tablename__ = "document_parse_versions"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "source_revision", "parse_revision", name="uq_document_parse_revision"
        ),
        UniqueConstraint("job_id", name="uq_document_parse_job"),
        UniqueConstraint("tenant_id", "project_id", "document_id", "id", name="uq_document_parse_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_document_parse_project_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "document_id"],
            ["documents.tenant_id", "documents.project_id", "documents.id"],
            name="fk_document_parse_document_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "job_id"],
            ["jobs.tenant_id", "jobs.project_id", "jobs.id"],
            name="fk_document_parse_job_scope",
        ),
        CheckConstraint("source_revision >= 1", name="ck_document_parse_source_revision"),
        CheckConstraint("parse_revision >= 0", name="ck_document_parse_parse_revision"),
        CheckConstraint("engine IN ('basic', 'docling', 'legacy')", name="ck_document_parse_engine"),
        CheckConstraint("origin IN ('worker', 'legacy')", name="ck_document_parse_origin"),
    )
    document_id: Mapped[str] = mapped_column(String(36))
    source_revision: Mapped[int] = mapped_column(Integer)
    parse_revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    ir_fingerprint: Mapped[str] = mapped_column(String(64))
    # Python None must not masquerade as JSON null in authoritative evidence.
    # The service validates the full IR, its fingerprint and source binding.
    ir: Mapped[dict] = mapped_column(JSON(none_as_null=True), nullable=False)
    parser: Mapped[str] = mapped_column(String(120))
    parser_version: Mapped[str] = mapped_column(String(120))
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)
    configuration_fingerprint: Mapped[str] = mapped_column(String(64))
    engine: Mapped[str] = mapped_column(String(20))
    origin: Mapped[str] = mapped_column(String(20))
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
