from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord


class ActionRun(ProjectRecord, Base):
    __tablename__ = "action_runs"
    __table_args__ = (UniqueConstraint("tenant_id", "project_id", "idempotency_key"),)
    action_key: Mapped[str] = mapped_column(String(100))
    target_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    target_revision: Mapped[int] = mapped_column(Integer)
    parameters: Mapped[dict] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    requester_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    approver_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="requested")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
