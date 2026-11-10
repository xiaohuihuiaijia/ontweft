from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord


class ChangeSet(ProjectRecord, Base):
    __tablename__ = "changesets"
    title: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    definition: Mapped[dict] = mapped_column(JSON)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    baseline_release_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    release_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Release(ProjectRecord, Base):
    __tablename__ = "releases"
    __table_args__ = (
        UniqueConstraint("project_id", "version"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_release_scope"),
    )
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict] = mapped_column(JSON)
    changeset_id: Mapped[str] = mapped_column(ForeignKey("changesets.id"))
    published_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
