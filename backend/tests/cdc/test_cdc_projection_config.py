"""Real configuration API and database guards; never runs a projector or worker."""

import copy
from contextlib import contextmanager
from uuid import uuid4

import pytest
from ontology.audit.models import Audit
from ontology.cdc.models import CdcEvent, CdcStream
from ontology.cdc.projection_models import CdcProjectionBinding, CdcProjectionState, CdcProjectionWindow
from ontology.identity.models import Project
from ontology.identity.security import ROLES
from ontology.jobs.models import Job
from ontology.modeling.models import Release
from ontology.objects.models import Entity
from ontology.sources.ingestion import schema_fingerprint
from ontology.sources.models import Source, SourceIdentity, SourceRecordRevision
from sqlalchemy import MetaData, Table, func, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.support.cdc import native_event
from tests.support.cdc_evidence_migrations import historical_stream, isolated_database, seed_old
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def catalog(resource_id="public.items"):
    result = {
        "resource_id": resource_id,
        "name": resource_id.split(".")[-1],
        "namespace": "public",
        "kind": "table",
        "fields": [
            {
                "name": "id",
                "data_type": "bigint",
                "nullable": False,
                "ordinal": 1,
                "metadata": {"udt_name": "int8"},
            },
            {
                "name": "name",
                "data_type": "text",
                "nullable": False,
                "ordinal": 2,
                "metadata": {"udt_name": "text"},
            },
        ],
        "primary_key": ["id"],
        "foreign_keys": [],
        "metadata": {},
    }
    result["schema_fingerprint"] = schema_fingerprint(result)
    return result


def prepare(platform, *, resources=None):
    client, app, owner, _ = platform
    resources = copy.deepcopy(resources or [catalog(), catalog("public.extra"), catalog("public.unmapped")])
    with app.state.db.transaction(owner["tenant_id"]) as session:
        source = Source(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            name="Synthetic discovered CDC source",
            kind="postgres",
            config={},
            resources=resources,
        )
        session.add(source)
        session.flush()
        source_id = source.id
    definition = {
        "types": [
            {
                "key": "Item",
                "label": "条目",
                "properties": [
                    {
                        "key": "name",
                        "label": "名称",
                        "data_type": "string",
                        "required": True,
                        "source_owned": True,
                    },
                ],
            }
        ],
        "mappings": [
            {
                "key": "map_" + str(index),
                "source_id": source_id,
                "resource_id": resource["resource_id"],
                "schema_fingerprint": schema_fingerprint(resource),
                "type_key": "Item",
                "fields": {"name": {"source_field": "name", "transform": "identity"}},
            }
            for index, resource in enumerate(resources[:2])
        ],
    }
    released = publish(client, owner["project_id"], definition)
    response = client.post("/api/cdc/streams", json={"source_id": source_id, "expected_source_revision": 1})
    assert response.status_code == 201, response.text
    stream = response.json()["stream"]
    payload = {
        "expected_release_id": released["release_id"],
        "expected_source_revision": 1,
        "resource_ids": [resource["resource_id"] for resource in resources[:2]],
    }
    return {
        "stream": stream,
        "source_id": source_id,
        "definition": definition,
        "resources": resources,
        "payload": payload,
        "url": f"/api/cdc/streams/{stream['id']}/projection",
    }


def freeze(client, setup):
    response = client.post(setup["url"], json=setup["payload"])
    assert response.status_code == 200, response.text
    return response.json()


@contextmanager
def rejected_constraint(sqlstate="23503"):
    with pytest.raises(DBAPIError) as result:
        yield result
    detail = result.value.orig.args[0]
    if isinstance(detail, dict):
        assert detail["C"] == sqlstate
    else:
        assert isinstance(result.value, IntegrityError)
        assert "constraint" in str(detail).lower()


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_configuration_freezes_exact_published_scope_and_is_idempotent(request, fixture_name):
    platform = request.getfixturevalue(fixture_name)
    client, app, owner, _ = platform
    setup = prepare(platform)
    initial = client.get(setup["url"])
    assert initial.status_code == 200
    assert initial.json()["status"] == "not_configured"
    assert initial.json()["can_configure"] and initial.json()["binding"] is None
    assert {item["resource_id"] for item in initial.json()["resources"] if item["covered"]} == {
        "public.items",
        "public.extra",
    }
    for override, status in (
        ({"expected_release_id": str(uuid4())}, 409),
        ({"expected_source_revision": 2}, 409),
        ({"resource_ids": ["public.items"]}, 422),
        ({"resource_ids": ["public.items", "public.extra", "public.unmapped"]}, 422),
        ({"resource_ids": ["public.items", "public.items"]}, 422),
        ({"resource_ids": ["public.items", "public.missing"]}, 422),
        ({"resource_ids": []}, 422),
    ):
        assert client.post(setup["url"], json={**setup["payload"], **override}).status_code == status
    configured = freeze(client, setup)
    binding = configured["binding"]
    assert binding["release_id"] == setup["payload"]["expected_release_id"]
    assert binding["generation"] == setup["stream"]["generation"] and binding["source_revision"] == 1
    assert binding["resource_ids"] == ["public.extra", "public.items"]
    assert configured["baseline"] is None
    assert configured["status"] == "waiting"
    assert configured["projected_sequence"] == configured["indexed_sequence"] == 0
    assert not configured["can_project"] and not configured["can_configure"]
    repeated = client.post(
        setup["url"], json={**setup["payload"], "resource_ids": ["public.extra", "public.items"]}
    )
    assert repeated.status_code == 200 and repeated.json()["binding"] == binding
    assert client.patch(setup["url"], json=setup["payload"]).status_code == 405
    with app.state.db.transaction(owner["tenant_id"]) as session:
        stored = session.scalar(select(CdcProjectionBinding))
        assert stored.resources == sorted(setup["resources"][:2], key=lambda item: item["resource_id"])
        assert session.scalar(select(func.count()).select_from(CdcProjectionBinding)) == 1
        assert session.scalar(select(func.count()).select_from(CdcProjectionState)) == 1
        assert (
            session.scalar(
                select(func.count()).select_from(Audit).where(Audit.event == "cdc.projection.configured")
            )
            == 1
        )
        assert session.scalar(select(func.count()).select_from(Entity)) == 0
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_binding_cannot_silently_follow_new_model_or_source_revision(platform):
    client, app, owner, _ = platform
    setup = prepare(platform)
    frozen = freeze(client, setup)["binding"]
    updated = copy.deepcopy(setup["definition"])
    updated["types"][0]["label"] = "新版条目"
    release = publish(client, owner["project_id"], updated)
    blocked = client.get(setup["url"]).json()
    assert blocked["reason_code"] == "binding_changed" and blocked["binding"] == frozen
    assert client.post(setup["url"], json=setup["payload"]).status_code == 409
    assert (
        client.post(
            setup["url"], json={**setup["payload"], "expected_release_id": release["release_id"]}
        ).status_code
        == 409
    )
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.get(Source, setup["source_id"]).revision += 1
    assert client.get(setup["url"]).json()["reason_code"] == "binding_changed"
    assert (
        client.post(setup["url"], json={**setup["payload"], "expected_source_revision": 2}).status_code == 409
    )
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(func.count()).select_from(CdcProjectionBinding)) == 1
        assert session.get(CdcProjectionBinding, frozen["id"]).source_revision == 1


@pytest.mark.parametrize("removed", ["sources:write", "ontology:write"])
def test_freezing_rechecks_both_current_permissions(platform, monkeypatch, removed):
    client, app, owner, _ = platform
    setup = prepare(platform)
    # Existing roles grant both together. Remove one permission from that actual
    # role policy to independently exercise each real authentication/access gate.
    monkeypatch.setitem(ROLES, "owner", ROLES["owner"] - {removed})
    response = client.post(setup["url"], json=setup["payload"])
    assert response.status_code == 403
    response = client.get(setup["url"])
    if removed == "sources:write":
        assert response.status_code == 403
    else:
        assert response.status_code == 200 and not response.json()["can_configure"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(func.count()).select_from(CdcProjectionBinding)) == 0


def seed_stream(session, owner, *, project_id=None, source_id=None):
    scope = {"tenant_id": owner["tenant_id"], "project_id": project_id or owner["project_id"]}
    if source_id is None:
        source = Source(**scope, name="Synthetic direct fixture source", kind="postgres", config={})
        session.add(source)
        session.flush()
        source_id = source.id
    stream = CdcStream(
        **scope,
        source_id=source_id,
        source_revision=1,
        generation=str(uuid4()),
        connector="postgresql",
        owner_id=owner["user_id"],
        token_hash=uuid4().hex * 2,
    )
    session.add(stream)
    session.flush()
    return stream


def test_configuration_rejects_other_tenant_and_unjoined_project(platform):
    client, app, owner, other = platform
    with app.state.db.transaction(other["tenant_id"]) as session:
        foreign_id = seed_stream(session, other).id
    with app.state.db.transaction(owner["tenant_id"]) as session:
        sibling = Project(tenant_id=owner["tenant_id"], name="No membership")
        session.add(sibling)
        session.flush()
        sibling_id = seed_stream(session, owner, project_id=sibling.id).id
    for stream_id, status in ((foreign_id, 404), (sibling_id, 404)):
        url = f"/api/cdc/streams/{stream_id}/projection"
        assert client.get(url).status_code == status
        assert (
            client.post(
                url,
                json={
                    "expected_release_id": str(uuid4()),
                    "expected_source_revision": 1,
                    "resource_ids": ["public.items"],
                },
            ).status_code
            == status
        )


@pytest.mark.parametrize(
    "metadata", [{"udt_name": "citext"}, {"udt_name": "bpchar"}, {"collation_deterministic": False}]
)
def test_configuration_rejects_unsupported_primary_key_equality(platform, metadata):
    resource = catalog()
    resource["fields"][0]["metadata"] = metadata
    setup = prepare(platform, resources=[resource])
    response = platform[0].post(setup["url"], json=setup["payload"])
    assert response.status_code == 422 and response.json()["error"]["code"] == "unsupported_key_semantics"


@pytest.mark.parametrize("invalid", ["missing_key_field", "duplicate_key", "duplicate_field"])
def test_configuration_requires_every_declared_primary_key_column_in_catalog(platform, invalid):
    resource = catalog()
    if invalid == "missing_key_field":
        resource["primary_key"] = ["id", "absent_key_column"]
    elif invalid == "duplicate_key":
        resource["primary_key"] = ["id", "id"]
    else:
        resource["fields"].append(copy.deepcopy(resource["fields"][0]))
    setup = prepare(platform, resources=[resource])
    response = platform[0].post(setup["url"], json=setup["payload"])
    assert response.status_code == 422, "An incomplete discovered primary key must not be frozen"


def window_values(owner, bound_stream_id, bound_binding_id, **overrides):
    return {
        "tenant_id": owner["tenant_id"],
        "project_id": owner["project_id"],
        "stream_id": bound_stream_id,
        "binding_id": bound_binding_id,
        "start_sequence": 1,
        "end_sequence": 2,
        "kind": "snapshot",
        "proof": {"kind": "test-only"},
        "schema_fingerprints": {},
        "result": {},
        "entity_ids": [],
        "source_position": {},
        **overrides,
    }


def test_postgres_projection_tables_force_rls_and_history_is_immutable(request):
    platform_fixture = request.getfixturevalue("pg_platform")
    database_fixture = request.getfixturevalue("pg_databases")
    client, app, owner, other = platform_fixture
    admin, _ = database_fixture
    setup = prepare(platform_fixture)
    binding_id = freeze(client, setup)["binding"]["id"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        window = CdcProjectionWindow(**window_values(owner, setup["stream"]["id"], binding_id))
        session.add(window)
        session.flush()
        window_id = window.id
        state = session.scalar(select(CdcProjectionState))
        state.reason_code = "allowed_mutable_progress"
    models = (CdcProjectionBinding, CdcProjectionState, CdcProjectionWindow)
    for tenant in (None, other["tenant_id"]):
        with app.state.db.transaction(tenant) as session:
            for model in models:
                assert session.scalars(select(model)).all() == []
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(CdcProjectionState.reason_code)) == "allowed_mutable_progress"
        for model in models:
            flags = session.execute(
                text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname=:name"),
                {"name": model.__tablename__},
            ).one()
            assert flags == (True, True)
    with rejected_constraint("42501"), app.state.db.transaction(other["tenant_id"]) as session:
        session.add(
            CdcProjectionWindow(**window_values(owner, setup["stream"]["id"], binding_id, end_sequence=3))
        )
        session.flush()
    for table, record_id in (("cdc_projection_bindings", binding_id), ("cdc_projection_windows", window_id)):
        for database in (app.state.db, admin):
            for statement in (
                f"DELETE FROM {table} WHERE id=:id",
                f"UPDATE {table} SET created_at=created_at WHERE id=:id",
            ):
                with (
                    rejected_constraint("42501") as denial,
                    database.transaction(owner["tenant_id"]) as session,
                ):
                    session.execute(text(statement), {"id": record_id})
                if database is admin:
                    assert "Immutable history" in denial.value.orig.args[0]["M"]


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_scoped_foreign_keys_reject_wrong_stream_source_release_and_window(request, fixture_name):
    platform = request.getfixturevalue(fixture_name)
    client, app, owner, _ = platform
    setup = prepare(platform)
    binding_id = freeze(client, setup)["binding"]["id"]
    sibling = client.post("/api/projects", json={"name": "Scope mismatch fixture"}).json()
    foreign_release = publish(client, sibling["id"], {"types": setup["definition"]["types"]})["release_id"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        foreign = seed_stream(session, owner, project_id=sibling["id"])
        foreign_source = foreign.source_id
        spare = seed_stream(session, owner)
        spare_values = {
            "tenant_id": owner["tenant_id"],
            "project_id": owner["project_id"],
            "stream_id": spare.id,
            "source_id": spare.source_id,
            "source_revision": 1,
            "generation": spare.generation,
            "release_id": setup["payload"]["expected_release_id"],
            "resources": setup["resources"][:2],
            "binding_hash": "a" * 64,
            "owner_id": owner["user_id"],
        }
        wrong_source = Source(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            name="Other same-project source",
            kind="postgres",
            config={},
        )
        session.add(wrong_source)
        session.flush()
        wrong_source_id = wrong_source.id
    for override in (
        {"project_id": sibling["id"]},
        {"source_id": foreign_source},
        {"source_id": wrong_source_id},
        {"release_id": foreign_release},
        {"source_revision": 2},
        {"generation": str(uuid4())},
    ):
        with rejected_constraint(), app.state.db.transaction(owner["tenant_id"]) as session:
            session.add(CdcProjectionBinding(**{**spare_values, **override}))
            session.flush()
    for override in ({"project_id": sibling["id"]}, {"stream_id": spare_values["stream_id"]}):
        with rejected_constraint(), app.state.db.transaction(owner["tenant_id"]) as session:
            session.add(
                CdcProjectionWindow(**window_values(owner, setup["stream"]["id"], binding_id, **override))
            )
            session.flush()
    with app.state.db.transaction(owner["tenant_id"]) as session:
        spare_binding = CdcProjectionBinding(**spare_values)
        session.add(spare_binding)
        session.flush()
        spare_binding_id = spare_binding.id
        own_window = CdcProjectionWindow(**window_values(owner, setup["stream"]["id"], binding_id))
        spare_window = CdcProjectionWindow(
            **window_values(owner, spare_values["stream_id"], spare_binding_id)
        )
        foreign_binding = CdcProjectionBinding(
            **{
                **spare_values,
                "project_id": sibling["id"],
                "stream_id": foreign.id,
                "source_id": foreign.source_id,
                "generation": foreign.generation,
                "release_id": foreign_release,
            }
        )
        session.add_all([own_window, spare_window, foreign_binding])
        session.flush()
        foreign_window = CdcProjectionWindow(
            **window_values(owner, foreign.id, foreign_binding.id, project_id=sibling["id"])
        )
        session.add(foreign_window)
        session.flush()
        own_window_id, spare_window_id, foreign_window_id = own_window.id, spare_window.id, foreign_window.id
        state = session.scalar(
            select(CdcProjectionState).where(CdcProjectionState.stream_id == setup["stream"]["id"])
        )
        state.baseline_window_id = own_window_id
    for invalid_baseline in (str(uuid4()), spare_window_id, foreign_window_id):
        with rejected_constraint(), app.state.db.transaction(owner["tenant_id"]) as session:
            state = session.scalar(
                select(CdcProjectionState).where(CdcProjectionState.stream_id == setup["stream"]["id"])
            )
            state.baseline_window_id = invalid_baseline
            session.flush()
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert (
            session.scalar(
                select(CdcProjectionState.baseline_window_id).where(
                    CdcProjectionState.stream_id == setup["stream"]["id"]
                )
            )
            == own_window_id
        )
    with rejected_constraint(), app.state.db.transaction(owner["tenant_id"]) as session:
        session.add(
            CdcProjectionState(
                tenant_id=owner["tenant_id"],
                project_id=sibling["id"],
                stream_id=spare_values["stream_id"],
                binding_id=spare_binding_id,
            )
        )
        session.flush()
    with rejected_constraint(), app.state.db.transaction(owner["tenant_id"]) as session:
        session.get(CdcStream, setup["stream"]["id"]).generation = str(uuid4())
        session.flush()
    # Source scope is enforced even before a projection binding exists.
    with rejected_constraint(), app.state.db.transaction(owner["tenant_id"]) as session:
        session.get(CdcStream, spare_values["stream_id"]).project_id = sibling["id"]
        session.flush()


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
@pytest.mark.parametrize("invalid_scope", [False, True])
def test_projection_migration_preserves_history_or_rejects_before_any_ddl(tmp_path, dialect, invalid_scope):
    with isolated_database(tmp_path, dialect) as (database, migrate, _):
        seeded = seed_old(database)
        migrate("8c4f2d9a6b13")
        owner = seeded["owner"]
        with database.transaction(owner["tenant_id"]) as session:
            stream = historical_stream(
                session,
                owner,
                source_id=seeded["source_id"],
                project_id=seeded["second_project"] if invalid_scope else owner["project_id"],
                received_sequence=1,
            )
            raw = native_event(stream.generation)
            session.add(
                CdcEvent(
                    tenant_id=owner["tenant_id"],
                    project_id=stream.project_id,
                    stream_id=stream.id,
                    sequence=1,
                    event_id=raw["event_id"],
                    identity_hash="b" * 64,
                    record_hash="c" * 64,
                    record=raw,
                )
            )
        models = (Source, Release, CdcStream, CdcEvent, SourceRecordRevision, SourceIdentity)

        def stored_rows():
            with database.transaction(owner["tenant_id"]) as session:
                return {
                    model.__tablename__: [
                        dict(row)
                        for row in session.execute(
                            select(
                                Table(model.__tablename__, MetaData(), autoload_with=session.connection())
                            ).order_by(text("id"))
                        ).mappings()
                    ]
                    for model in models
                }

        previous = stored_rows()
        migrate("b9e5217ac340", succeeds=not invalid_scope)
        assert stored_rows() == previous
        with database.transaction(owner["tenant_id"]) as session:
            version = session.scalar(text("SELECT version_num FROM alembic_version"))
            tables = inspect(session.connection()).get_table_names()
            if invalid_scope:
                assert version == "8c4f2d9a6b13"
                assert "cdc_projection_bindings" not in tables
                assert "uq_source_scope" not in {
                    item["name"] for item in inspect(session.connection()).get_unique_constraints("sources")
                }
            else:
                assert version == "b9e5217ac340"
                assert "cdc_projection_bindings" in tables
                assert "fk_cdc_stream_source_scope" in {
                    item["name"] for item in inspect(session.connection()).get_foreign_keys("cdc_streams")
                }
