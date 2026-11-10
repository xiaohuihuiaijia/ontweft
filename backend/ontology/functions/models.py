"""Versioned trusted functions and append-only computation records."""

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord
from ontology.identity.models import Project as Project  # noqa: PLC0414 - frozen runtime export
from ontology.identity.models import User as User  # noqa: PLC0414 - frozen runtime export
from ontology.objects.models import Entity as Entity  # noqa: PLC0414 - frozen runtime export


class FunctionVersion(ProjectRecord, Base):
    __tablename__ = "function_versions"
    __table_args__ = (UniqueConstraint("project_id", "key", "version"),)
    key: Mapped[str] = mapped_column(String(80))
    label: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    kernel_key: Mapped[str] = mapped_column(String(80))
    kernel_version: Mapped[int] = mapped_column(Integer)
    bindings: Mapped[dict] = mapped_column(JSON)
    unit: Mapped[str] = mapped_column(String(80))
    baseline_release_id: Mapped[str] = mapped_column(ForeignKey("releases.id"))
    artifact: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class FunctionRun(ProjectRecord, Base):
    __tablename__ = "function_runs"
    function_version_id: Mapped[str] = mapped_column(ForeignKey("function_versions.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    model_release_id: Mapped[str] = mapped_column(ForeignKey("releases.id"))
    request_hash: Mapped[str] = mapped_column(String(64))
    artifact_hash: Mapped[str] = mapped_column(String(64))
    inputs: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
