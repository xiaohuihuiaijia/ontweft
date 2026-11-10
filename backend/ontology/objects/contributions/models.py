"""Current source contributions and immutable source-by-source revisions."""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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


class ContributionValues:
    identity_id: Mapped[str] = mapped_column(ForeignKey("source_identities.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    record_id: Mapped[str] = mapped_column(ForeignKey("source_record_revisions.id"))
    record_codec: Mapped[str] = mapped_column(String(40))
    release_id: Mapped[str] = mapped_column(ForeignKey("releases.id"))
    mapping_key: Mapped[str] = mapped_column(String(80))
    mapped_values: Mapped[dict] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    withdraw_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    native_row: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    stream_id: Mapped[str | None] = mapped_column(ForeignKey("cdc_streams.id"), nullable=True)
    sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


def scoped_constraints(prefix):
    return (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "identity_id"],
            ["source_identities.tenant_id", "source_identities.project_id", "source_identities.id"],
            name=f"fk_{prefix}_identity_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "record_id", "record_codec"],
            [
                "source_record_revisions.tenant_id",
                "source_record_revisions.project_id",
                "source_record_revisions.id",
                "source_record_revisions.record_codec",
            ],
            name=f"fk_{prefix}_record_scope",
        ),
        CheckConstraint("revision >= 1", name=f"ck_{prefix}_revision"),
        CheckConstraint(
            "(active AND withdraw_reason IS NULL) OR (NOT active AND withdraw_reason IS NOT NULL AND length(withdraw_reason) > 0)",
            name=f"ck_{prefix}_withdrawal",
        ),
        CheckConstraint(
            "(record_codec = 'snapshot-json-v1' AND stream_id IS NULL AND sequence IS NULL AND native_row IS NULL) OR "
            "(record_codec = 'cdc-native-reference-v1' AND stream_id IS NOT NULL AND sequence IS NOT NULL AND sequence >= 1)",
            name=f"ck_{prefix}_origin",
        ),
    )


class SourceContribution(ContributionValues, ProjectRecord, Base):
    __tablename__ = "source_contributions"
    __table_args__ = (
        UniqueConstraint("identity_id", name="uq_contribution_identity"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_contribution_scope"),
        *scoped_constraints("contribution"),
    )


class ContributionRevision(ContributionValues, ProjectRecord, Base):
    __tablename__ = "contribution_revisions"
    contribution_id: Mapped[str] = mapped_column(ForeignKey("source_contributions.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (
        UniqueConstraint("contribution_id", "revision", name="uq_contribution_revision"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "contribution_id"],
            ["source_contributions.tenant_id", "source_contributions.project_id", "source_contributions.id"],
            name="fk_contribution_revision_scope",
        ),
        *scoped_constraints("contribution_revision"),
    )
