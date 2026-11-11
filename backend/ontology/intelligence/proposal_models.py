"""AI output remains reviewable data; it is never an active ontology release."""

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord


class AIProposal(ProjectRecord, Base):
    __tablename__ = "ai_proposals"
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    connection_id: Mapped[str] = mapped_column(ForeignKey("model_connections.id"))
    connection_revision: Mapped[int] = mapped_column(Integer)
    baseline_release_id: Mapped[str | None] = mapped_column(ForeignKey("releases.id"), nullable=True)
    inputs: Mapped[dict] = mapped_column(JSON)
    definition: Mapped[dict] = mapped_column(JSON, default=lambda: {"types": [], "mappings": []})
    candidates: Mapped[list] = mapped_column(JSON, default=list)
    relations: Mapped[list] = mapped_column(JSON, default=list)
    gaps: Mapped[list] = mapped_column(JSON, default=list)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    changeset_id: Mapped[str | None] = mapped_column(ForeignKey("changesets.id"), nullable=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    imported: Mapped[dict] = mapped_column(JSON, default=dict)


class AIPage(ProjectRecord, Base):
    """Immutable model interpretation of one page, independently reusable across goals."""
    __tablename__ = "ai_document_pages"
    __table_args__ = (UniqueConstraint("tenant_id", "project_id", "cache_key", name="uq_ai_page_cache"),)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    source_revision: Mapped[int] = mapped_column(Integer)
    page_number: Mapped[int] = mapped_column(Integer)
    cache_key: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(200))
    result: Mapped[dict] = mapped_column(JSON)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)


class DocumentRelationContribution(ProjectRecord, Base):
    __tablename__ = "document_relation_contributions"
    __table_args__ = (UniqueConstraint("source_id", "fact_key", name="uq_document_relation_fact"),)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    fact_key: Mapped[str] = mapped_column(String(64))
    proposal_id: Mapped[str] = mapped_column(ForeignKey("ai_proposals.id"))
    relation_id: Mapped[str] = mapped_column(ForeignKey("relations.id"), index=True)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
