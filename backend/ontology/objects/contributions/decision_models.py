"""Immutable, version-bound human decisions over source contribution fields."""

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord


class ContributionDecision(ProjectRecord, Base):
    __tablename__ = "contribution_decisions"
    __table_args__ = (
        UniqueConstraint("entity_id", "field_key", "revision", name="uq_contribution_decision_revision"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_contribution_decision_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "entity_id"],
            ["entities.tenant_id", "entities.project_id", "entities.id"],
            name="fk_contribution_decision_entity_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "selected_contribution_id"],
            ["source_contributions.tenant_id", "source_contributions.project_id", "source_contributions.id"],
            name="fk_contribution_decision_source_scope",
        ),
        ForeignKeyConstraint(
            ["selected_contribution_id", "selected_contribution_revision"],
            ["contribution_revisions.contribution_id", "contribution_revisions.revision"],
            name="fk_contribution_decision_source_revision",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "model_release_id"],
            ["releases.tenant_id", "releases.project_id", "releases.id"],
            name="fk_contribution_decision_release_scope",
        ),
        CheckConstraint("revision >= 1", name="ck_contribution_decision_revision"),
        CheckConstraint("entity_revision >= 1", name="ck_contribution_decision_entity_revision"),
        CheckConstraint(
            "operation IN ('select', 'hold', 'clear')", name="ck_contribution_decision_operation"
        ),
        CheckConstraint(
            "(operation = 'select' AND selected_contribution_id IS NOT NULL "
            "AND selected_contribution_revision IS NOT NULL AND selected_contribution_revision >= 1) OR "
            "(operation IN ('hold', 'clear') AND selected_contribution_id IS NULL "
            "AND selected_contribution_revision IS NULL)",
            name="ck_contribution_decision_selection",
        ),
        CheckConstraint("CAST(basis AS TEXT) <> 'null'", name="ck_contribution_decision_basis_not_null"),
        CheckConstraint(
            "length(reason) <= 2000 AND "
            "length(trim(replace(replace(replace(reason, '\t', ''), '\n', ''), '\r', ''))) > 0",
            name="ck_contribution_decision_reason",
        ),
        CheckConstraint("length(field_key) BETWEEN 1 AND 80", name="ck_contribution_decision_field"),
        CheckConstraint("length(basis_fingerprint) = 64", name="ck_contribution_decision_basis_hash"),
        CheckConstraint("length(preview_fingerprint) = 64", name="ck_contribution_decision_preview_hash"),
    )
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    field_key: Mapped[str] = mapped_column(String(80))
    revision: Mapped[int] = mapped_column(Integer)
    operation: Mapped[str] = mapped_column(String(20))
    selected_contribution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    selected_contribution_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_release_id: Mapped[str] = mapped_column(String(36))
    entity_revision: Mapped[int] = mapped_column(Integer)
    # The service verifies the full basis and fingerprints; neither Python None
    # nor explicit JSON null is a valid authoritative review snapshot.
    basis: Mapped[dict] = mapped_column(JSON(none_as_null=True), nullable=False)
    basis_fingerprint: Mapped[str] = mapped_column(String(64))
    preview_fingerprint: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(2000))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
