"""Decision storage constraints and non-fabricating migrations on both databases."""

import hashlib
import json
from uuid import uuid4

import pytest
from ontology.identity.models import Project
from ontology.modeling.models import ChangeSet, Release
from ontology.objects.contributions.decision_models import ContributionDecision
from ontology.objects.contributions.models import ContributionRevision, SourceContribution
from ontology.objects.models import Entity
from ontology.sources.models import Source, SourceIdentity, SourceRecordRevision, SourceSnapshot
from sqlalchemy import JSON, MetaData, Table, delete, select, text, update
from sqlalchemy.exc import DBAPIError

from tests.support.cdc_evidence_migrations import constraint_rejection, isolated_database, seed_old
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration

HEAD = "a7d83e2c190f"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def parents(db, owner, project_id=None):
    """Seed real parent/history rows without coupling storage tests to adjudication."""
    scope = {"tenant_id": owner["tenant_id"], "project_id": project_id or owner["project_id"]}
    with db.transaction(owner["tenant_id"]) as session:
        change = ChangeSet(
            **scope, title="Decision storage fixture", definition={"types": []}, author_id=owner["user_id"]
        )
        source = Source(**scope, name="Synthetic contribution source", kind="sqlite")
        session.add_all([change, source])
        session.flush()
        release = Release(
            **scope,
            version=1,
            definition=change.definition,
            changeset_id=change.id,
            published_by=owner["user_id"],
        )
        session.add(release)
        session.flush()
        snapshot = SourceSnapshot(
            **scope,
            source_id=source.id,
            source_revision=1,
            resource_id="main.items",
            schema_fingerprint="a" * 64,
            release_id=release.id,
            status="complete",
            attempt=1,
            record_count=1,
            consistency="transaction",
        )
        entity = Entity(
            **scope,
            type_key="Item",
            properties={"quantity": "12345678901234567890.00001"},
            release_id=release.id,
        )
        session.add_all([snapshot, entity])
        session.flush()
        raw = {"key": {"id": 9223372036854775807}, "values": entity.properties}
        record = SourceRecordRevision(
            **scope,
            snapshot_id=snapshot.id,
            record_codec="snapshot-json-v1",
            attempt=1,
            source_key=raw["key"],
            key_hash=digest(raw["key"]),
            values=raw["values"],
            content_hash=digest(raw),
        )
        session.add(record)
        session.flush()
        identity = SourceIdentity(
            **scope,
            source_id=source.id,
            resource_id=snapshot.resource_id,
            key_hash=record.key_hash,
            type_key=entity.type_key,
            entity_id=entity.id,
            last_record_id=record.id,
            last_record_codec=record.record_codec,
            mapping_key="items",
        )
        session.add(identity)
        session.flush()
        contribution = SourceContribution(
            **scope,
            identity_id=identity.id,
            revision=1,
            record_id=record.id,
            record_codec=record.record_codec,
            release_id=release.id,
            mapping_key=identity.mapping_key,
            mapped_values=dict(entity.properties),
        )
        session.add(contribution)
        session.flush()
        add_history(session, contribution, owner["user_id"])
        basis = {
            "model_release_id": release.id,
            "property": {"key": "quantity", "label": "数量😀", "data_type": "number", "source_owned": True},
            "contributions": [
                {
                    "id": contribution.id,
                    "revision": 1,
                    "record_id": record.id,
                    "active": True,
                    "value": raw["values"]["quantity"],
                    "source_key": raw["key"],
                }
            ],
        }
        return {
            **scope,
            "entity_id": entity.id,
            "field_key": "quantity",
            "revision": 1,
            "operation": "select",
            "selected_contribution_id": contribution.id,
            "selected_contribution_revision": 1,
            "model_release_id": release.id,
            "entity_revision": entity.revision,
            "basis": basis,
            "basis_fingerprint": digest(basis),
            "preview_fingerprint": digest({"basis": basis, "operation": "select"}),
            "reason": "人工核对原始凭据后采纳此来源，保留另一方历史。",
            "actor_id": owner["user_id"],
        }


def add_history(session, current, actor_id):
    session.add(
        ContributionRevision(
            tenant_id=current.tenant_id,
            project_id=current.project_id,
            contribution_id=current.id,
            actor_id=actor_id,
            **{
                key: getattr(current, key)
                for key in (
                    "identity_id",
                    "revision",
                    "record_id",
                    "record_codec",
                    "release_id",
                    "mapping_key",
                    "mapped_values",
                    "active",
                    "withdraw_reason",
                    "native_row",
                    "stream_id",
                    "sequence",
                )
            },
        )
    )


@pytest.fixture(params=["platform", "pg_platform"])
def decision_fixture(request):
    _, app, owner, other = request.getfixturevalue(request.param)
    return app.state.db, owner, other, parents(app.state.db, owner)


def test_decision_operations_round_trip_exact_basis_without_mutating_source(decision_fixture):
    db, owner, _, fields = decision_fixture
    with db.transaction(owner["tenant_id"]) as session:
        for revision, operation in enumerate(("select", "hold", "clear"), 1):
            row = {
                **fields,
                "revision": revision,
                "operation": operation,
                "selected_contribution_id": fields["selected_contribution_id"]
                if operation == "select"
                else None,
                "selected_contribution_revision": 1 if operation == "select" else None,
            }
            session.add(ContributionDecision(**row))
    with db.transaction(owner["tenant_id"]) as session:
        rows = session.scalars(select(ContributionDecision).order_by(ContributionDecision.revision)).all()
        assert [row.operation for row in rows] == ["select", "hold", "clear"]
        assert all(row.basis == fields["basis"] for row in rows)
        assert all(row.reason == fields["reason"] and row.actor_id == owner["user_id"] for row in rows)
        assert rows[0].selected_contribution_revision == 1
        assert rows[1].selected_contribution_id is None and rows[2].selected_contribution_revision is None
        assert session.get(SourceContribution, fields["selected_contribution_id"]).revision == 1
        assert session.get(Entity, fields["entity_id"]).revision == 1


def test_decision_checks_reject_invalid_operations_basis_and_partial_selection(decision_fixture):
    db, owner, _, fields = decision_fixture
    for changes in (
        {"revision": 0},
        {"entity_revision": 0},
        {"operation": "overwrite"},
        {"selected_contribution_id": None},
        {"selected_contribution_revision": None},
        {"selected_contribution_revision": 0},
        {"selected_contribution_id": None, "selected_contribution_revision": None},
        {"operation": "hold"},
        {"operation": "clear", "selected_contribution_revision": None},
        {"basis": None},  # Python None binds SQL NULL, not an accepted JSON null.
        {"basis": JSON.NULL},
        {"reason": ""},
        {"reason": " \t\r\n "},
        {"field_key": ""},
        {"basis_fingerprint": "short"},
        {"preview_fingerprint": None},
        {"actor_id": str(uuid4())},
    ):
        with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
            session.add(ContributionDecision(**{**fields, **changes}))
            session.flush()
    # PostgreSQL enforces VARCHAR limits before CHECK constraints; SQLite
    # needs the explicit length checks. Assert each dialect's precise failure.
    for changes in ({"reason": "x" * 2001}, {"field_key": "x" * 81}):
        with pytest.raises(DBAPIError) as rejected, db.transaction(owner["tenant_id"]) as session:
            session.add(ContributionDecision(**{**fields, **changes}))
            session.flush()
        if db.engine.dialect.name == "postgresql":
            assert rejected.value.orig.args[0]["C"] == "22001"
        else:
            assert "CHECK constraint failed" in str(rejected.value.orig)
    with db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(ContributionDecision.id)) is None


def test_decision_selection_binds_existing_immutable_revision_not_mutable_current(decision_fixture):
    db, owner, _, fields = decision_fixture
    with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
        session.add(ContributionDecision(**{**fields, "selected_contribution_revision": 2}))
        session.flush()
    with db.transaction(owner["tenant_id"]) as session:
        decision = ContributionDecision(**fields)
        session.add(decision)
        session.flush()
        identifier = decision.id
    with db.transaction(owner["tenant_id"]) as session:
        current = session.get(SourceContribution, fields["selected_contribution_id"])
        current.revision = 2
        current.mapped_values = {"quantity": None}
        add_history(session, current, owner["user_id"])
    with db.transaction(owner["tenant_id"]) as session:
        row = session.get(ContributionDecision, identifier)
        old = session.scalar(
            select(ContributionRevision).where(
                ContributionRevision.contribution_id == row.selected_contribution_id,
                ContributionRevision.revision == row.selected_contribution_revision,
            )
        )
        assert old.revision == 1 and old.mapped_values["quantity"] == "12345678901234567890.00001"
        assert row.basis == fields["basis"]
        session.add(ContributionDecision(**{**fields, "revision": 2, "selected_contribution_revision": 2}))


def test_decision_revision_uniqueness_rolls_back_other_writes(decision_fixture):
    db, owner, _, fields = decision_fixture
    with db.transaction(owner["tenant_id"]) as session:
        session.add(ContributionDecision(**fields))
    with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
        entity = session.get(Entity, fields["entity_id"])
        entity.revision = 2
        entity.properties = {"quantity": "999"}
        session.flush()
        session.add(ContributionDecision(**{**fields, "reason": "Duplicate decision must fail atomically"}))
        session.flush()
    with db.transaction(owner["tenant_id"]) as session:
        entity = session.get(Entity, fields["entity_id"])
        assert entity.revision == 1 and entity.properties["quantity"] == "12345678901234567890.00001"
        session.add(ContributionDecision(**{**fields, "field_key": "other_field"}))
        session.add(ContributionDecision(**{**fields, "revision": 2}))


def test_decision_scope_rejects_cross_project_tenant_and_unknown_references(decision_fixture):
    db, owner, other, fields = decision_fixture
    with db.transaction(owner["tenant_id"]) as session:
        sibling = Project(tenant_id=owner["tenant_id"], name="Separate decision project")
        session.add(sibling)
        session.flush()
        sibling_id = sibling.id
    same_tenant = parents(db, owner, sibling_id)
    other_tenant = parents(db, other)
    for reference in (same_tenant, other_tenant):
        for name in ("entity_id", "model_release_id", "selected_contribution_id"):
            with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
                session.add(ContributionDecision(**{**fields, name: reference[name]}))
                session.flush()
    for changes in (
        {"project_id": sibling_id},
        {"entity_id": str(uuid4())},
        {"model_release_id": str(uuid4())},
        {"selected_contribution_id": str(uuid4())},
        {
            "project_id": other["project_id"],
            "operation": "hold",
            "selected_contribution_id": None,
            "selected_contribution_revision": None,
        },
    ):
        with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
            session.add(ContributionDecision(**{**fields, **changes}))
            session.flush()


def test_postgres_decision_rls_runtime_grants_and_owner_immutability(request):
    _, app, owner, other = request.getfixturevalue("pg_platform")
    admin, _ = request.getfixturevalue("pg_databases")
    db = app.state.db
    fields = parents(db, owner)
    with db.transaction(owner["tenant_id"]) as session:
        row = ContributionDecision(**fields)
        session.add(row)
        session.flush()
        identifier = row.id
        assert session.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='contribution_decisions'"
            )
        ).one() == (True, True)
        assert session.execute(
            text(
                "SELECT has_table_privilege(current_user, 'contribution_decisions', 'SELECT'), "
                "has_table_privilege(current_user, 'contribution_decisions', 'INSERT'), "
                "has_table_privilege(current_user, 'contribution_decisions', 'UPDATE'), "
                "has_table_privilege(current_user, 'contribution_decisions', 'DELETE'), "
                "has_table_privilege(current_user, 'contribution_decisions', 'TRUNCATE')"
            )
        ).one() == (True, True, False, False, False)
    for tenant in (None, other["tenant_id"]):
        with db.transaction(tenant) as session:
            assert session.scalars(select(ContributionDecision)).all() == []
    with pytest.raises(DBAPIError) as rejected, db.transaction(other["tenant_id"]) as session:
        session.add(ContributionDecision(**{**fields, "revision": 2}))
        session.flush()
    assert rejected.value.orig.args[0]["C"] == "42501"
    for connection_db in (db, admin):
        for statement in (
            update(ContributionDecision)
            .where(ContributionDecision.id == identifier)
            .values(reason="Tampered"),
            delete(ContributionDecision).where(ContributionDecision.id == identifier),
        ):
            with (
                pytest.raises(DBAPIError) as rejected,
                connection_db.transaction(owner["tenant_id"]) as session,
            ):
                session.execute(statement)
            assert rejected.value.orig.args[0]["C"] == "42501"
            if connection_db is admin:
                assert "Immutable history" in rejected.value.orig.args[0]["M"]
    with db.transaction(owner["tenant_id"]) as session:
        assert session.get(ContributionDecision, identifier).reason == fields["reason"]


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_decision_migration_preserves_ledger_originals_and_does_not_invent_reviews(tmp_path, dialect):
    with isolated_database(tmp_path, dialect) as (db, migrate, _):
        legacy = seed_old(db)
        migrate("f2c7d9a41b60")
        # The existing legacy owner's project already has a release; use its
        # separate project for a populated current ledger before the migration.
        fields = parents(db, legacy["owner"], legacy["second_project"])
        names = (
            "entities",
            "source_identities",
            "source_contributions",
            "contribution_revisions",
            "source_record_revisions",
            "source_snapshots",
            "releases",
            "entity_revisions",
        )
        with db.transaction(legacy["owner"]["tenant_id"]) as session:
            before = {}
            for name in names:
                table = Table(name, MetaData(), autoload_with=session.connection())
                before[name] = [
                    dict(row) for row in session.execute(select(table).order_by(table.c.id)).mappings()
                ]
        migrate(HEAD)
        with db.transaction(legacy["owner"]["tenant_id"]) as session:
            for name in names:
                table = Table(name, MetaData(), autoload_with=session.connection())
                after = [dict(row) for row in session.execute(select(table).order_by(table.c.id)).mappings()]
                assert after == before[name]
            assert session.scalar(select(ContributionDecision.id)) is None
            if dialect == "sqlite":
                assert session.execute(text("PRAGMA foreign_key_check")).all() == []
            else:
                assert (
                    session.scalar(
                        text(
                            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                            "WHERE c.relname='contribution_decisions' AND t.tgname='immutable_history'"
                        )
                    )
                    == 1
                )
        # Exercise migrated SQLite too, rather than only ORM create_all.
        with db.transaction(legacy["owner"]["tenant_id"]) as session:
            session.add(ContributionDecision(**fields))
        for changes in (
            {},
            {"revision": 2, "selected_contribution_revision": 999},
            {"revision": 2, "basis": JSON.NULL},
        ):
            with constraint_rejection(), db.transaction(legacy["owner"]["tenant_id"]) as session:
                session.add(ContributionDecision(**{**fields, **changes}))
                session.flush()
