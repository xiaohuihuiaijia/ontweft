from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord


class RelationRule(ProjectRecord, Base):
    __tablename__ = "relation_rules"
    name: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    cursor: Mapped[str] = mapped_column(String(36), default="")
    indexed: Mapped[bool] = mapped_column(Boolean, default=False)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class RelationMatchIndex(ProjectRecord, Base):
    __tablename__ = "relation_match_index"
    __table_args__ = (
        UniqueConstraint("rule_id", "side", "entity_id", name="uq_rule_match_entity"),
        Index("ix_rule_match_value", "rule_id", "side", "value_hash", "entity_id"),
    )
    rule_id: Mapped[str] = mapped_column(ForeignKey("relation_rules.id"))
    side: Mapped[str] = mapped_column(String(6))
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    value_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RelationWork(ProjectRecord, Base):
    __tablename__ = "relation_work"
    __table_args__ = (
        UniqueConstraint("rule_id", "entity_id", name="uq_rule_work_entity"),
        Index("ix_rule_work_pending", "rule_id", "status", "id"),
    )
    rule_id: Mapped[str] = mapped_column(ForeignKey("relation_rules.id"))
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    status: Mapped[str] = mapped_column(String(12), default="pending")
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class RelationContribution(ProjectRecord, Base):
    __tablename__ = "relation_contributions"
    __table_args__ = (UniqueConstraint("rule_id", "source_id", name="uq_rule_contribution_source"),)
    rule_id: Mapped[str] = mapped_column(ForeignKey("relation_rules.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    relation_id: Mapped[str] = mapped_column(ForeignKey("relations.id"), index=True)
