from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord


class KnowledgePage(ProjectRecord, Base):
    __tablename__ = "knowledge_pages"
    __table_args__ = (UniqueConstraint("tenant_id", "project_id", "id", name="uq_knowledge_page_scope"),)
    title: Mapped[str] = mapped_column(String(250))
    body: Mapped[str] = mapped_column(Text, default="")
    references: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    published_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))


class KnowledgeRevision(ProjectRecord, Base):
    __tablename__ = "knowledge_revisions"
    __table_args__ = (
        UniqueConstraint("page_id", "revision"),
        UniqueConstraint("tenant_id", "project_id", "page_id", "id", name="uq_knowledge_revision_page_scope"),
    )
    page_id: Mapped[str] = mapped_column(ForeignKey("knowledge_pages.id"))
    revision: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(250))
    body: Mapped[str] = mapped_column(Text)
    references: Mapped[list] = mapped_column(JSON)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
