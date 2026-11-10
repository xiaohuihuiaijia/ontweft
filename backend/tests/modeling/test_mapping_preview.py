"""Saved-sample mapping diagnostics and authority/version boundaries."""

import copy
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from ontology.core.models import now
from ontology.identity.models import Membership, Project
from ontology.modeling.models import ChangeSet
from ontology.sources.ingestion import fingerprint, schema_fingerprint
from ontology.sources.models import Source, SourceRecordRevision, SourceSnapshot
from sqlalchemy import event, select

from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def seed(app, owner):
    """Persist real rows; no source driver is used by this preview fixture."""
    resource = {
        "resource_id": "inventory",
        "kind": "table",
        "primary_key": ["id"],
        "foreign_keys": [],
        "fields": [
            {"name": key, "data_type": "string"}
            for key in ("id", "name", "quantity", "amount", "optional", "missing", "private")
        ],
    }
    with app.state.db.transaction(owner["tenant_id"]) as session:
        source = Source(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            name="preview-source",
            kind="sqlite",
            resources=[resource],
            config={"private": "never-echo-configuration"},
            secret="never-echo-secret",
            revision=3,
        )
        session.add(source)
        session.flush()
        mapping = {
            "key": "stock_mapping",
            "source_id": source.id,
            "resource_id": "inventory",
            "schema_fingerprint": schema_fingerprint(resource),
            "type_key": "Stock",
            "fields": {
                "name": {"source_field": "name", "transform": "trim"},
                "quantity": {"source_field": "quantity", "transform": "integer"},
                "amount": {"source_field": "amount", "transform": "number"},
                "optional": {"source_field": "optional", "transform": "identity"},
            },
        }
        definition = {
            "types": [
                {
                    "key": "Stock",
                    "label": "Stock",
                    "properties": [
                        {
                            "key": "name",
                            "label": "Name",
                            "data_type": "string",
                            "required": True,
                            "source_owned": True,
                        },
                        {
                            "key": "quantity",
                            "label": "Quantity",
                            "data_type": "integer",
                            "source_owned": True,
                        },
                        {"key": "amount", "label": "Amount", "data_type": "number", "source_owned": True},
                        {"key": "optional", "label": "Optional", "data_type": "string", "source_owned": True},
                    ],
                }
            ],
            "mappings": [mapping],
        }
        change = ChangeSet(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            title="Saved mapping draft",
            author_id=owner["user_id"],
            definition=definition,
            revision=2,
        )
        snapshot = SourceSnapshot(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            source_id=source.id,
            source_revision=3,
            resource_id="inventory",
            schema_fingerprint=schema_fingerprint(resource),
            status="complete",
            attempt=4,
            record_count=2,
            consistency="transaction",
            completed_at=now(),
        )
        session.add_all([change, snapshot])
        session.flush()
        rows = []
        values = [
            {
                "id": 9223372036854775807,
                "name": " A ",
                "quantity": 900719925474099312345678901,
                "amount": "12345678901234567890.12345678901234567890",
                "optional": None,
                "private": "never-read-unmapped-private-value",
            },
            {"id": 2, "name": None, "amount": "not-a-number", "optional": "ok"},
        ]
        for value in values:
            row = SourceRecordRevision(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                snapshot_id=snapshot.id,
                attempt=4,
                source_key={"id": value["id"]},
                key_hash=fingerprint(value["id"]),
                values=value,
                content_hash=fingerprint(value),
            )
            session.add(row)
            rows.append(row)
        session.flush()
        result = {
            "changeset_id": change.id,
            "expected_revision": change.revision,
            "mapping_key": mapping["key"],
            "snapshot_id": snapshot.id,
            "expected_snapshot_attempt": snapshot.attempt,
            "record_ids": [row.id for row in rows],
        }
        return result, source.id


@pytest.fixture
def sample(platform):
    client, app, owner, other = platform
    data, source_id = seed(app, owner)
    return client, app, owner, other, data, source_id


def post(client, data):
    return client.post("/api/mappings/preview", json=data)


def test_exact_values_missing_null_all_errors_and_no_business_reads_or_writes(sample):
    client, app, owner, _, data, source_id = sample
    with app.state.db.transaction(owner["tenant_id"]) as session:
        before = [
            session.get(ChangeSet, data["changeset_id"]).revision,
            session.get(Source, source_id).revision,
            session.get(SourceSnapshot, data["snapshot_id"]).attempt,
        ]

    def forbid_business_access(_conn, _cursor, statement, _params, _context, _many):
        assert not re.search(r"\b(entities|entity_revisions|source_identities|relations)\b", statement)
        assert not re.match(r"\s*(INSERT|UPDATE|DELETE)\b", statement, re.IGNORECASE)

    event.listen(app.state.db.engine, "before_cursor_execute", forbid_business_access)
    try:
        response = post(client, data)
    finally:
        event.remove(app.state.db.engine, "before_cursor_execute", forbid_business_access)
    assert response.status_code == 200, response.text
    value = response.json()
    assert [row["record_id"] for row in value["rows"]] == data["record_ids"]
    assert value["summary"] == {
        "scope": "selected_records_only",
        "selected_count": 2,
        "valid_count": 1,
        "invalid_count": 1,
        "capture_complete": True,
        "writes": 0,
        "message": "仅验证选中样本；不代表全量验证或对象投影结果。",
    }
    first, second = value["rows"]
    fields = {f["target_field"]: f for f in first["fields"]}
    assert fields["quantity"]["transformed_value_json"] == "900719925474099312345678901"
    assert fields["quantity"]["raw_value_json"] == "900719925474099312345678901"
    assert fields["quantity"]["validated_value_json"] == "900719925474099312345678901"
    assert fields["amount"]["validated_value_json"] == '"12345678901234567890.12345678901234567890"'
    assert fields["optional"]["source_state"] == "null" and fields["optional"]["raw_value_json"] == "null"
    assert json.loads(first["properties_json"])["name"] == "A"
    bad = {f["target_field"]: f for f in second["fields"]}
    assert bad["quantity"]["source_state"] == "missing" and bad["quantity"]["raw_value_json"] is None
    assert bad["quantity"]["errors"][0]["stage"] == "source"
    assert bad["name"]["errors"][0]["stage"] == "type"
    assert bad["amount"]["errors"][0]["stage"] == "transform"
    assert second["properties_json"] is None
    assert "never-" not in response.text
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert before == [
            session.get(ChangeSet, data["changeset_id"]).revision,
            session.get(Source, source_id).revision,
            session.get(SourceSnapshot, data["snapshot_id"]).attempt,
        ]


def test_failed_persisted_samples_can_diagnose_partial_capture(sample):
    client, app, owner, _, data, _ = sample
    with app.state.db.transaction(owner["tenant_id"]) as session:
        snapshot = session.get(SourceSnapshot, data["snapshot_id"])
        snapshot.status, snapshot.completed_at, snapshot.consistency = "failed", None, "unknown"
    result = post(client, data)
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["binding"]["snapshot_status"] == "failed"
    assert value["binding"]["consistency"] == "unknown"
    assert value["summary"]["capture_complete"] is False
    assert "不证明完整采集" in value["summary"]["message"]


def test_old_snapshot_release_remains_usable_under_current_draft(sample):
    from tests.support.platform import publish

    client, app, owner, _, data, _ = sample
    # Original snapshot has no release; a later unrelated model is published.
    published = publish(client, owner["project_id"], {"types": []})
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.get(ChangeSet, data["changeset_id"]).baseline_release_id = published["release_id"]
    response = post(client, data)
    assert response.status_code == 200, response.text
    assert response.json()["binding"]["snapshot_release_id"] is None
    assert response.json()["binding"]["baseline_release_id"] == published["release_id"]


@pytest.mark.parametrize(
    "failure", ["revision", "published", "attempt", "source_revision", "schema", "resource", "baseline"]
)
def test_stale_or_mismatched_context_rejected(sample, failure):
    client, app, owner, _, data, source_id = sample
    if failure == "baseline":
        from tests.support.platform import publish

        publish(client, owner["project_id"], {"types": []})
        assert post(client, data).status_code == 409
        return
    with app.state.db.transaction(owner["tenant_id"]) as session:
        change = session.get(ChangeSet, data["changeset_id"])
        snapshot = session.get(SourceSnapshot, data["snapshot_id"])
        source = session.get(Source, source_id)
        if failure == "revision":
            change.revision += 1
        elif failure == "published":
            change.status = "published"
        elif failure == "attempt":
            snapshot.attempt += 1
        elif failure == "source_revision":
            source.revision += 1
        elif failure == "schema":
            resource = copy.deepcopy(source.resources)
            resource[0]["fields"].append({"name": "new", "data_type": "string"})
            source.resources = resource
        elif failure == "resource":
            snapshot.resource_id = "other-resource"
    assert post(client, data).status_code == 409


@pytest.mark.parametrize("state", ["pending", "capturing", "cancelled"])
def test_nonterminal_snapshots_cannot_be_previewed(sample, state):
    client, app, owner, _, data, _ = sample
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.get(SourceSnapshot, data["snapshot_id"]).status = state
    assert post(client, data).status_code == 422


def test_old_attempt_records_and_incomplete_selection_are_not_silently_dropped(sample):
    client, app, owner, _, data, _ = sample
    assert post(client, {**data, "record_ids": [data["record_ids"][0], "unknown-id"]}).status_code == 404
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.get(SourceSnapshot, data["snapshot_id"]).attempt = 5
    assert post(client, {**data, "expected_snapshot_attempt": 5}).status_code == 404


@pytest.mark.parametrize("selection", [[], ["duplicate", "duplicate"], [str(i) for i in range(21)]])
def test_selection_is_explicit_unique_and_bounded(sample, selection):
    client, _, _, _, data, _ = sample
    assert post(client, {**data, "record_ids": selection}).status_code == 422


@pytest.mark.parametrize("field", ["changeset_id", "snapshot_id", "record_ids"])
def test_cross_tenant_references_return_no_partial_data(sample, field):
    client, app, _, other, data, _ = sample
    hidden, _ = seed(app, other)
    assert post(client, {**data, field: hidden[field]}).status_code == 404


def test_same_tenant_different_project_snapshot_rejected(sample):
    client, app, owner, _, data, _ = sample
    with app.state.db.transaction(owner["tenant_id"]) as session:
        project = Project(tenant_id=owner["tenant_id"], name="Other project")
        session.add(project)
        session.flush()
        other_owner = {**owner, "project_id": project.id}
    other, _ = seed(app, other_owner)
    assert post(client, {**data, "snapshot_id": other["snapshot_id"]}).status_code == 404


@pytest.mark.parametrize("missing_permission", ["ontology:write", "sources:write"])
def test_requires_both_permissions(sample, monkeypatch, missing_permission):
    from ontology.identity.security import ROLES

    client, app, owner, _, data, _ = sample
    # Test independent capability checks, not only today's overlapping modeler role.
    monkeypatch.setitem(ROLES, "modeler", ROLES["modeler"] - {missing_permission})
    with app.state.db.transaction(owner["tenant_id"]) as session:
        member = session.scalar(select(Membership).where(Membership.user_id == owner["user_id"]))
        member.role = "modeler"
    assert post(client, data).status_code == 403


def test_feishu_identity_is_not_bypassed_by_persisted_snapshot(sample):
    client, app, owner, _, data, source_id = sample
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.get(Source, source_id).kind = "feishu"
    response = post(client, data)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "employee_authorization_required"


def test_invalid_mapping_is_distinct_from_sample_type_errors(sample):
    client, app, owner, _, data, _ = sample
    with app.state.db.transaction(owner["tenant_id"]) as session:
        change = session.get(ChangeSet, data["changeset_id"])
        definition = copy.deepcopy(change.definition)
        definition["mappings"][0]["fields"]["quantity"]["source_field"] = "undiscovered"
        change.definition = definition
    response = post(client, data)
    assert response.status_code == 422 and response.json()["error"]["code"] == "mapping_invalid"


def test_output_budget_rejects_instead_of_truncating_evidence(sample, monkeypatch):
    from ontology.sources import mapping_preview

    client, _, _, _, data, _ = sample
    monkeypatch.setattr(mapping_preview, "MAX_PREVIEW_BYTES", 10)
    response = post(client, data)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "preview_budget"


def test_postgres_saved_preview_is_tenant_scoped_and_diagnostic(request):
    client, app, owner, other = request.getfixturevalue("pg_platform")
    data, _ = seed(app, owner)
    hidden, _ = seed(app, other)
    result = post(client, data)
    assert result.status_code == 200, result.text
    assert result.json()["summary"]["valid_count"] == 1
    assert post(client, {**data, "record_ids": hidden["record_ids"]}).status_code == 404


def test_real_snapshot_worker_output_is_previewed_without_reopening_source(platform, monkeypatch):
    import ontology.sources.api as sources_api

    from tests.support.ingestion import capture, make_source

    client, app, owner, _ = platform
    source, resource, _, definition = make_source(client, app, owner["project_id"])
    captured = capture(client, app, source, resource)
    assert captured["snapshot"]["status"] == "complete"
    baseline = client.get("/api/ontology/definition", params={"project_id": owner["project_id"]}).json()[
        "release_id"
    ]
    saved = client.post(
        "/api/changesets",
        json={
            "project_id": owner["project_id"],
            "title": "Preview saved worker evidence",
            "definition": definition,
            "expected_baseline_release_id": baseline,
        },
    )
    assert saved.status_code == 201, saved.text

    def forbidden_connector(*_):
        raise AssertionError("Preview must not reopen a live source")

    monkeypatch.setattr(sources_api, "connector_for", forbidden_connector)
    data = {
        "changeset_id": saved.json()["id"],
        "expected_revision": saved.json()["revision"],
        "mapping_key": definition["mappings"][0]["key"],
        "snapshot_id": captured["snapshot"]["id"],
        "expected_snapshot_attempt": captured["snapshot"]["attempt"],
        "record_ids": [row["id"] for row in captured["items"]],
    }
    result = post(client, data)
    assert result.status_code == 200, result.text
    assert result.json()["summary"]["valid_count"] == 2
    assert {json.loads(row["properties_json"])["quantity"] for row in result.json()["rows"]} == {80, 20}


def test_postgres_snapshot_attempt_is_refreshed_after_row_lock(request):
    client, app, owner, _ = request.getfixturevalue("pg_platform")
    data, _ = seed(app, owner)
    waiting = threading.Event()
    main_thread = threading.get_ident()

    def observe_lock(_conn, _cursor, statement, _params, _context, _many):
        if (
            threading.get_ident() != main_thread
            and "source_snapshots" in statement
            and "FOR UPDATE" in statement
        ):
            waiting.set()

    event.listen(app.state.db.engine, "before_cursor_execute", observe_lock)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with app.state.db.transaction(owner["tenant_id"]) as session:
                snapshot = session.scalar(
                    select(SourceSnapshot)
                    .where(
                        SourceSnapshot.id == data["snapshot_id"],
                    )
                    .with_for_update()
                )
                future = pool.submit(post, client, data)
                assert waiting.wait(5), "Preview did not acquire the snapshot row lock"
                assert not future.done()
                snapshot.attempt, snapshot.status = 5, "capturing"
            response = future.result(timeout=10)
            assert response.status_code == 409, response.text
    finally:
        event.remove(app.state.db.engine, "before_cursor_execute", observe_lock)
