from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from ontology.cdc.decode import DecodedRow, FieldValue, ValueState
from ontology.cdc.models import CdcStream
from ontology.cdc.values import decode_row, encode_row
from ontology.core.errors import DomainError
from ontology.modeling.models import ChangeSet, Release
from ontology.objects.contributions.models import ContributionRevision, SourceContribution
from ontology.objects.contributions.service import ensure_legacy_contributions
from ontology.objects.models import Entity, EntityRevision
from ontology.sources.models import Source, SourceIdentity, SourceRecordRevision
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from tests.support.contributions import apply, loaded, native_record, raw_record, seed
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def test_legacy_bootstrap_uses_immutable_evidence_and_is_idempotent(platform):
    _, app, owner, _ = platform
    ids = seed(app.state.db, owner)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        ctx, entity, identity, _ = loaded(session, owner, ids)
        original = deepcopy(entity.properties), entity.revision, identity.last_record_id
        assert ensure_legacy_contributions(session, ctx, entity) == 1
        assert ensure_legacy_contributions(session, ctx, entity) == 0
        current = session.scalar(select(SourceContribution))
        assert current.mapped_values == {"name": "A", "quantity": "10.00"}
        assert "note" not in current.mapped_values
        assert (entity.properties, entity.revision, identity.last_record_id) == original
        assert len(session.scalars(select(ContributionRevision)).all()) == 1


def test_unproved_legacy_value_is_rejected_without_fabricating_contribution(platform):
    _, app, owner, _ = platform
    ids = seed(app.state.db, owner)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        entity = session.get(Entity, ids["entity"])
        entity.properties = {**entity.properties, "quantity": "999"}
    with (
        pytest.raises(DomainError, match="人工审核"),
        app.state.db.transaction(owner["tenant_id"]) as session,
    ):
        ctx, entity, _, _ = loaded(session, owner, ids)
        ensure_legacy_contributions(session, ctx, entity)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(SourceContribution)) is None
        assert session.scalar(select(ContributionRevision)) is None


def test_existing_identity_requires_explicit_bootstrap_and_same_values_keep_semantic_revision(platform):
    _, app, owner, _ = platform
    ids = seed(app.state.db, owner)
    with (
        pytest.raises(DomainError, match="先从不可变旧证据"),
        app.state.db.transaction(owner["tenant_id"]) as session,
    ):
        ctx, _, identity, release = loaded(session, owner, ids)
        apply(
            session,
            ctx,
            identity,
            session.get(SourceRecordRevision, ids["record"]),
            release,
            values={"name": "A", "quantity": "10.00"},
        )
    with app.state.db.transaction(owner["tenant_id"]) as session:
        ctx, entity, identity, release = loaded(session, owner, ids)
        ensure_legacy_contributions(session, ctx, entity)
        before = deepcopy(entity.evidence), entity.revision
        record = raw_record(
            session,
            owner,
            session.get(Source, ids["sources"][0]),
            release,
            {"name": "A", "quantity": "10.0"},
            generation=2,
        )
        result = apply(session, ctx, identity, record, release, values=record.values)
        assert result["changed"] is False
        assert (entity.evidence, entity.revision) == before
        assert identity.last_record_id == record.id
        assert result["contribution"].revision == 2
        assert (
            apply(session, ctx, identity, record, release, values=record.values)["contribution"].revision == 2
        )
        assert len(session.scalars(select(ContributionRevision)).all()) == 2


def test_multiple_contributions_conflict_and_withdrawal_preserve_other_source_and_manual_values(platform):
    _, app, owner, _ = platform
    ids = seed(app.state.db, owner)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        ctx, entity, identity, release = loaded(session, owner, ids)
        ensure_legacy_contributions(session, ctx, entity)
        source2 = session.get(Source, ids["sources"][1])
        record2 = raw_record(session, owner, source2, release, {"name": "A", "quantity": "10.0"})
        identity2 = SourceIdentity(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            source_id=source2.id,
            resource_id="inventory",
            type_key="Stock",
            entity_id=entity.id,
            key_hash=record2.key_hash,
            last_record_id=record2.id,
            mapping_key="mapping1",
        )
        session.add(identity2)
        session.flush()
        same = apply(
            session, ctx, identity2, record2, release, index=1, values=record2.values, new_identity=True
        )
        assert same["changed"] is False and entity.data_status == "active"
        changed = raw_record(session, owner, source2, release, {"name": "A", "quantity": "12"}, generation=2)
        apply(session, ctx, identity2, changed, release, index=1, values=changed.values)
        assert entity.data_status == "conflict"
        assert entity.properties == {"name": "A", "note": "manual retained"}
        values = [row.mapped_values["quantity"] for row in session.scalars(select(SourceContribution))]
        assert sorted(values) == ["10.00", "12"]
        apply(session, ctx, identity2, changed, release, index=1, withdraw_reason="source row deleted")
        assert entity.data_status == "active" and entity.properties["quantity"] == "10.00"
        assert entity.properties["note"] == "manual retained"
        apply(
            session,
            ctx,
            identity,
            session.get(SourceRecordRevision, ids["record"]),
            release,
            withdraw_reason="source disconnected",
        )
        assert entity.data_status == "withdrawn" and entity.deleted is False
        assert entity.properties == {"note": "manual retained"}
        history = session.scalars(
            select(EntityRevision)
            .where(EntityRevision.entity_id == entity.id)
            .order_by(EntityRevision.revision)
        ).all()
        assert [row.data_status for row in history] == ["active", "conflict", "active", "withdrawn"]
        withdrawn = session.scalars(
            select(ContributionRevision).where(ContributionRevision.active.is_(False))
        ).all()
        assert len(withdrawn) == 2 and all(row.mapped_values["name"] == "A" for row in withdrawn)


def test_null_is_an_active_value_not_source_withdrawal(platform):
    _, app, owner, _ = platform
    ids = seed(app.state.db, owner)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        ctx, entity, identity, release = loaded(session, owner, ids)
        ensure_legacy_contributions(session, ctx, entity)
        record = raw_record(
            session,
            owner,
            session.get(Source, ids["sources"][0]),
            release,
            {"name": "A", "quantity": None},
            generation=2,
        )
        result = apply(session, ctx, identity, record, release, values=record.values)
        assert result["contribution"].active and result["contribution"].withdraw_reason is None
        assert entity.data_status == "active" and entity.properties["quantity"] is None


def test_project_scope_cannot_attach_foreign_record(platform):
    _, app, owner, other = platform
    ids, hidden = seed(app.state.db, owner), seed(app.state.db, other)
    with pytest.raises(DomainError), app.state.db.transaction(owner["tenant_id"]) as session:
        ctx, entity, identity, release = loaded(session, owner, ids)
        ensure_legacy_contributions(session, ctx, entity)
        apply(
            session,
            ctx,
            identity,
            session.get(SourceRecordRevision, hidden["record"]),
            release,
            values={"name": "A", "quantity": "10"},
        )


def test_new_entity_first_materialization_keeps_revision_one(platform):
    _, app, owner, _ = platform
    ids = seed(app.state.db, owner)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        ctx, _, _, release = loaded(session, owner, ids)
        record = raw_record(
            session, owner, session.get(Source, ids["sources"][1]), release, {"name": "New", "quantity": "12"}
        )
        entity = Entity(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            type_key="Stock",
            properties={},
            evidence=[],
            release_id=release.id,
        )
        session.add(entity)
        session.flush()
        identity = SourceIdentity(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            source_id=ids["sources"][1],
            resource_id="inventory",
            type_key="Stock",
            entity_id=entity.id,
            key_hash=record.key_hash,
            last_record_id=record.id,
            mapping_key="mapping1",
        )
        session.add(identity)
        session.flush()
        result = apply(
            session,
            ctx,
            identity,
            record,
            release,
            index=1,
            values=record.values,
            new_identity=True,
            new_entity=True,
        )
        assert result["changed"] and entity.revision == 1 and entity.data_status == "active"
        history = session.scalars(select(EntityRevision).where(EntityRevision.entity_id == entity.id)).all()
        assert len(history) == 1 and history[0].revision == 1 and history[0].properties["name"] == "New"
        with pytest.raises(DomainError, match="首次物化"):
            apply(
                session,
                ctx,
                identity,
                record,
                release,
                index=1,
                values=record.values,
                new_identity=True,
                new_entity=True,
            )


def test_partial_mapping_marks_incomplete_until_other_contribution_supplies_required_value(platform):
    _, app, owner, _ = platform
    ids = seed(app.state.db, owner)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        ctx, entity, identity, release = loaded(session, owner, ids)
        ensure_legacy_contributions(session, ctx, entity)
        definition = deepcopy(release.definition)
        definition["mappings"][0]["fields"].pop("name")
        change = ChangeSet(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            title="Partial source",
            definition=definition,
            author_id=owner["user_id"],
            status="published",
        )
        session.add(change)
        session.flush()
        release2 = Release(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            version=2,
            definition=definition,
            changeset_id=change.id,
            published_by=owner["user_id"],
        )
        session.add(release2)
        session.flush()
        ctx.project.active_release_id, ctx.project.version = release2.id, 2
        record = raw_record(
            session,
            owner,
            session.get(Source, ids["sources"][0]),
            release2,
            {"name": "A", "quantity": "11"},
            generation=2,
        )
        apply(session, ctx, identity, record, release2, values={"quantity": "11"})
        assert entity.data_status == "incomplete" and entity.properties == {
            "quantity": "11",
            "note": "manual retained",
        }
        record2 = raw_record(
            session, owner, session.get(Source, ids["sources"][1]), release2, {"name": "A", "quantity": "11"}
        )
        identity2 = SourceIdentity(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            source_id=ids["sources"][1],
            resource_id="inventory",
            type_key="Stock",
            entity_id=entity.id,
            key_hash=record2.key_hash,
            last_record_id=record2.id,
            mapping_key="mapping1",
        )
        session.add(identity2)
        session.flush()
        apply(session, ctx, identity2, record2, release2, index=1, values=record2.values, new_identity=True)
        assert entity.data_status == "active" and entity.properties["name"] == "A"
        apply(session, ctx, identity2, record2, release2, index=1, withdraw_reason="second source removed")
        assert entity.data_status == "incomplete" and entity.properties["quantity"] == "11"


def test_native_reference_uses_typed_row_and_ordered_event_without_rewriting_raw(platform):
    _, app, owner, _ = platform
    ids = seed(app.state.db, owner)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        ctx, entity, identity, release = loaded(session, owner, ids)
        ensure_legacy_contributions(session, ctx, entity)
        stream = CdcStream(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            source_id=ids["sources"][0],
            source_revision=1,
            generation=str(uuid4()),
            connector="postgresql",
            owner_id=owner["user_id"],
            token_hash="a" * 64,
        )
        session.add(stream)
        session.flush()
        first, second = native_record(session, owner, stream, 1), native_record(session, owner, stream, 2)
        encoded = encode_row(
            DecodedRow({"quantity": FieldValue(ValueState.VALUE, Decimal("12.00000000000000001"))})
        )
        result = apply(
            session,
            ctx,
            identity,
            second,
            release,
            values={"name": "A", "quantity": "12.00000000000000001"},
            native_row=encoded,
            stream_id=stream.id,
            sequence=2,
        )
        assert result["contribution"].native_row == encoded
        assert decode_row(result["contribution"].native_row).fields["quantity"].value == Decimal(
            "12.00000000000000001"
        )
        assert (
            identity.last_record_codec == "cdc-native-reference-v1" and identity.last_record_id == second.id
        )
        assert second.values is None and second.snapshot_id is None
        with pytest.raises(DomainError, match="旧 CDC"):
            apply(
                session,
                ctx,
                identity,
                first,
                release,
                values={"name": "A", "quantity": "10"},
                stream_id=stream.id,
                sequence=1,
            )
        with pytest.raises(DomainError, match="类型编码"):
            apply(
                session,
                ctx,
                identity,
                second,
                release,
                values={"name": "A", "quantity": "12"},
                native_row={"quantity": 12},
                stream_id=stream.id,
                sequence=2,
            )
        apply(
            session,
            ctx,
            identity,
            second,
            release,
            stream_id=stream.id,
            sequence=2,
            withdraw_reason="row deleted",
        )
        assert entity.data_status == "withdrawn" and not entity.deleted
        assert result["contribution"].native_row == encoded


def test_postgres_atomicity_rls_immutable_revision_and_scoped_fk(request):
    _, app, owner, other = request.getfixturevalue("pg_platform")
    ids, hidden = seed(app.state.db, owner), seed(app.state.db, other)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        ctx, entity, _, _ = loaded(session, owner, ids)
        ensure_legacy_contributions(session, ctx, entity)
    with app.state.db.transaction(other["tenant_id"]) as session:
        ctx, entity, _, _ = loaded(session, other, hidden)
        ensure_legacy_contributions(session, ctx, entity)
    with (
        pytest.raises(RuntimeError, match="after materialization"),
        app.state.db.transaction(owner["tenant_id"]) as session,
    ):
        ctx, entity, identity, release = loaded(session, owner, ids)
        record = raw_record(
            session,
            owner,
            session.get(Source, ids["sources"][0]),
            release,
            {"name": "changed", "quantity": "20"},
            generation=2,
        )
        apply(session, ctx, identity, record, release, values=record.values)
        assert entity.properties["name"] == "changed"
        raise RuntimeError("after materialization")
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.get(Entity, ids["entity"]).properties["name"] == "A"
        assert session.get(SourceIdentity, ids["identity"]).last_record_id == ids["record"]
        assert len(session.scalars(select(SourceContribution)).all()) == 1
        assert len(session.scalars(select(ContributionRevision)).all()) == 1
        assert session.scalar(select(SourceContribution)).revision == 1
        # No tenant WHERE: FORCE RLS still hides the other tenant's ledger.
        assert session.scalar(select(SourceContribution)).identity_id == ids["identity"]
    with app.state.db.transaction() as session:
        assert session.scalars(select(SourceContribution)).all() == []
        assert session.scalars(select(ContributionRevision)).all() == []
    for command in ("UPDATE contribution_revisions SET active=false", "DELETE FROM contribution_revisions"):
        with pytest.raises(DBAPIError), app.state.db.transaction(owner["tenant_id"]) as session:
            session.execute(text(command))
        # Prove the trigger too, independently of the runtime's INSERT-only grant.
        admin, _ = request.getfixturevalue("pg_databases")
        with pytest.raises(DBAPIError, match="Immutable"), admin.transaction(owner["tenant_id"]) as session:
            session.execute(text(command))
    # A valid same-tenant record FK cannot be replaced by a known foreign ID.
    with pytest.raises(DBAPIError) as rejected, app.state.db.transaction(owner["tenant_id"]) as session:
        row = session.scalar(select(SourceContribution))
        row.record_id = hidden["record"]
        session.flush()
    assert rejected.value.orig.args[0]["C"] == "23503"
    assert rejected.value.orig.args[0]["n"] == "fk_contribution_record_scope"
