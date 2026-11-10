"""Action execution records; additive to the existing ActionRun model."""

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord


class ActionSnapshot(ProjectRecord, Base):
    __tablename__ = "action_snapshots"
    __table_args__ = (UniqueConstraint("run_id"),)
    run_id: Mapped[str] = mapped_column(ForeignKey("action_runs.id"), index=True)
    frozen: Mapped[dict] = mapped_column(JSON)
    approval_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    executor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class ActionOutbox(ProjectRecord, Base):
    __tablename__ = "action_outbox"
    __table_args__ = (UniqueConstraint("run_id"),)
    run_id: Mapped[str] = mapped_column(ForeignKey("action_runs.id"), index=True)
    state: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[float] = mapped_column(Float)


class ActionAttempt(ProjectRecord, Base):
    __tablename__ = "action_attempts"
    run_id: Mapped[str] = mapped_column(ForeignKey("action_runs.id"), index=True)
    event: Mapped[str] = mapped_column(String(80))
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
