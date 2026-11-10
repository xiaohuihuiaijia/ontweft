from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord, now


class Entity(ProjectRecord, Base):
    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_entity_scope"),
        Index("ix_entity_type", "tenant_id", "project_id", "type_key"),
        CheckConstraint(
            "data_status IN ('active', 'conflict', 'incomplete', 'withdrawn')", name="ck_entity_data_status"
        ),
    )
    type_key: Mapped[str] = mapped_column(String(120))
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    release_id: Mapped[str] = mapped_column(ForeignKey("releases.id"))
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    data_status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")


class EntityRevision(ProjectRecord, Base):
    __tablename__ = "entity_revisions"
    __table_args__ = (UniqueConstraint("entity_id", "revision"),)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    properties: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[list] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text, default="")
    data_status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")


class Relation(ProjectRecord, Base):
    __tablename__ = "relations"
    __table_args__ = (UniqueConstraint("project_id", "type_key", "source_id", "target_id"),)
    type_key: Mapped[str] = mapped_column(String(120))
    source_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    target_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
