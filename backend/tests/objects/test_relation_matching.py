from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from ontology.audit.models import Audit
from ontology.identity.models import Membership
from sqlalchemy import select

from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


def setup_model(client, owner, *, cardinality="one", kind="string", protected=False):
    definition = {
        "types": [
            {
                "key": "Item",
                "label": "条目",
                "properties": [
                    {"key": "name", "label": "名称", "data_type": "string"},
                    {
                        "key": "reference",
                        "label": "关联编号",
                        "data_type": kind,
                        "read_roles": ["owner"] if protected else [],
                    },
                ],
                "relations": [
                    {"key": "belongs_to", "label": "所属", "target_type": "Group", "cardinality": cardinality}
                ],
            },
            {
                "key": "Group",
                "label": "分组",
                "properties": [
                    {"key": "name", "label": "名称", "data_type": "string"},
                    {"key": "code", "label": "编号", "data_type": kind},
                ],
                "relations": [],
            },
        ]
    }
    publish(client, owner["project_id"], definition)
    release = client.get("/api/ontology/definition", params={"project_id": owner["project_id"]}).json()[
        "release_id"
    ]
    return {
        "project_id": owner["project_id"],
        "expected_model_release_id": release,
        "source_type": "Item",
        "relation_key": "belongs_to",
        "source_field": "reference",
        "target_field": "code",
    }


def obj(client, rule, type_key, value):
    response = client.post(
        "/api/objects",
        json={
            "project_id": rule["project_id"],
            "expected_model_release_id": rule["expected_model_release_id"],
            "type_key": type_key,
            "properties": {"name": "合成对象", "reference" if type_key == "Item" else "code": value},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def preview(client, rule):
    response = client.post("/api/relations/match-preview", json=rule)
    assert response.status_code == 200, response.text
    return response.json()


def apply(client, rule, plan, **extra):
    return client.post(
        "/api/relations/match-apply", json={"rule": rule, "expected_plan_hash": plan["plan_hash"], **extra}
    )


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_explicit_match_preview_and_apply_are_scoped_versioned_and_idempotent(request, fixture_name):
    client, app, owner, other = request.getfixturevalue(fixture_name)
    rule = setup_model(client, owner)
    source = obj(client, rule, "Item", "001")
    target = obj(client, rule, "Group", "001")
    plan = preview(client, rule)
    assert plan["counts"]["create"] == 1 and plan["unresolved_count"] == 0
    assert client.get("/api/relations", params={"project_id": owner["project_id"]}).json()["total"] == 0
    result = apply(client, rule, plan)
    assert result.status_code == 200, result.text
    assert result.json()["created"] == 1
    assert apply(client, rule, plan).status_code == 409
    repeated = apply(client, rule, preview(client, rule))
    assert repeated.json()["created"] == 0 and repeated.json()["existing"] == 1
    rows = client.get("/api/relations", params={"project_id": owner["project_id"]}).json()["items"]
    assert len(rows) == 1 and rows[0]["source_id"] == source["id"] and rows[0]["target_id"] == target["id"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        events = session.scalars(
            select(Audit).where(Audit.project_id == owner["project_id"], Audit.event == "relations.matched")
        ).all()
        created = next(event.details["created"] for event in events if event.details["created"])
        assert created[0]["source_revision"] == source["revision"]
        assert created[0]["target_revision"] == target["revision"]
    assert (
        client.post(
            "/api/relations/match-preview", json={**rule, "project_id": other["project_id"]}
        ).status_code
        == 404
    )


def test_unmatched_ambiguous_and_missing_values_require_explicit_partial_choice(platform):
    client, _, owner, _ = platform
    rule = setup_model(client, owner)
    for value in ["known", "unknown", "duplicate", None]:
        obj(client, rule, "Item", value)
    for value in ["known", "duplicate", "duplicate"]:
        obj(client, rule, "Group", value)
    plan = preview(client, rule)
    assert plan["counts"] == {
        "create": 1,
        "existing": 0,
        "missing_value": 1,
        "unmatched": 1,
        "ambiguous": 1,
        "self_excluded": 0,
        "cardinality_conflict": 0,
    }
    assert apply(client, rule, plan).status_code == 422
    assert client.get("/api/relations", params={"project_id": owner["project_id"]}).json()["total"] == 0
    result = apply(client, rule, plan, allow_partial=True)
    assert result.status_code == 200 and result.json()["created"] == 1 and result.json()["unresolved"] == 3


def test_changed_object_invalidates_preview_without_partial_writes(platform):
    client, _, owner, _ = platform
    rule = setup_model(client, owner)
    obj(client, rule, "Item", "A")
    target = obj(client, rule, "Group", "A")
    plan = preview(client, rule)
    update = client.patch(
        f"/api/objects/{target['id']}",
        json={"expected_revision": target["revision"], "properties": {"code": "B"}},
    )
    assert update.status_code == 200
    assert apply(client, rule, plan).status_code == 409
    assert client.get("/api/relations", params={"project_id": owner["project_id"]}).json()["total"] == 0


def test_one_cardinality_does_not_replace_an_existing_manual_relation(platform):
    client, _, owner, _ = platform
    rule = setup_model(client, owner)
    source = obj(client, rule, "Item", "A")
    obj(client, rule, "Group", "A")
    other = obj(client, rule, "Group", "B")
    existing = client.post(
        "/api/relations",
        json={
            "project_id": owner["project_id"],
            "type_key": "belongs_to",
            "source_id": source["id"],
            "target_id": other["id"],
        },
    )
    assert existing.status_code == 201
    plan = preview(client, rule)
    assert plan["counts"]["cardinality_conflict"] == 1
    assert apply(client, rule, plan).status_code == 422
    assert apply(client, rule, plan, allow_partial=True).json()["created"] == 0
    assert (
        client.get("/api/relations", params={"project_id": owner["project_id"]}).json()["items"][0][
            "target_id"
        ]
        == other["id"]
    )


def test_text_matching_preserves_zeros_whitespace_case_and_decimal_matching_is_exact(platform):
    client, _, owner, _ = platform
    rule = setup_model(client, owner)
    for value in ["001", "a", "A "]:
        obj(client, rule, "Item", value)
    for value in ["1", "A", "A"]:
        obj(client, rule, "Group", value)
    assert preview(client, rule)["counts"]["unmatched"] == 3


def test_exact_decimal_values_match_without_float_rounding(platform):
    client, _, owner, _ = platform
    rule = setup_model(client, owner, kind="number")
    obj(client, rule, "Item", "9007199254740993.20")
    obj(client, rule, "Group", "9007199254740993.2")
    obj(client, rule, "Group", "9007199254740992.2")
    plan = preview(client, rule)
    assert plan["counts"]["create"] == 1 and plan["counts"]["ambiguous"] == 0


def test_hidden_match_fields_are_rechecked_on_apply(platform):
    client, app, owner, _ = platform
    rule = setup_model(client, owner, protected=True)
    obj(client, rule, "Item", "sensitive")
    obj(client, rule, "Group", "sensitive")
    plan = preview(client, rule)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        member = session.scalar(select(Membership).where(Membership.project_id == owner["project_id"]))
        member.role = "operator"
    result = apply(client, rule, plan)
    assert result.status_code == 403 and "sensitive" not in result.text
    assert client.post("/api/relations/match-preview", json=rule).status_code == 403
    assert (
        client.post(
            "/api/relations/match-issues", json={"rule": rule, "expected_plan_hash": plan["plan_hash"]}
        ).status_code
        == 403
    )


def test_matching_budget_rejects_without_silent_truncation(platform, monkeypatch):
    client, _, owner, _ = platform
    rule = setup_model(client, owner)
    for value in ["A", "B", "C"]:
        obj(client, rule, "Item", value)
    monkeypatch.setattr("ontology.objects.relation_matching.MAX_OBJECTS", 2)
    response = client.post("/api/relations/match-preview", json=rule)
    assert response.status_code == 422 and response.json()["error"]["code"] == "match_budget"


def test_issue_pages_cover_all_unresolved_objects_and_reject_stale_baselines(platform):
    client, _, owner, _ = platform
    rule = setup_model(client, owner)
    sources = [obj(client, rule, "Item", f"missing-{index}") for index in range(27)]
    plan = preview(client, rule)
    assert plan["unresolved_count"] == 27 and len(plan["issues"]) == 25
    query = {"rule": rule, "expected_plan_hash": plan["plan_hash"], "offset": 25, "limit": 25}
    response = client.post("/api/relations/match-issues", json=query)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 27 and page["offset"] == 25 and len(page["rows"]) == 2
    returned = plan["issues"] + page["rows"]
    assert len({row["source_id"] for row in returned}) == 27
    assert {row["source_id"] for row in returned} == {row["id"] for row in sources}
    assert all(row["status"] == "unmatched" for row in returned)
    assert client.post("/api/relations/match-issues", json={**query, "limit": 1000}).status_code == 422
    edited = client.patch(
        f"/api/objects/{sources[0]['id']}",
        json={
            "expected_revision": sources[0]["revision"],
            "properties": {"reference": "changed"},
        },
    )
    assert edited.status_code == 200
    assert client.post("/api/relations/match-issues", json=query).status_code == 409


def test_self_relations_require_explicit_choice_and_old_model_baselines_are_rejected(platform):
    client, _, owner, _ = platform
    rule = setup_model(client, owner)
    model = client.get("/api/ontology/definition", params={"project_id": owner["project_id"]}).json()[
        "definition"
    ]
    model["types"][0]["relations"][0]["target_type"] = "Item"
    publish(client, owner["project_id"], model)
    assert client.post("/api/relations/match-preview", json=rule).status_code == 409
    rule["expected_model_release_id"] = client.get(
        "/api/ontology/definition", params={"project_id": owner["project_id"]}
    ).json()["release_id"]
    rule["target_field"] = "reference"
    item = obj(client, rule, "Item", "self")
    assert preview(client, rule)["counts"]["self_excluded"] == 1
    rule["allow_self"] = True
    plan = preview(client, rule)
    assert plan["counts"]["create"] == 1
    assert apply(client, rule, plan).status_code == 200
    row = client.get("/api/relations", params={"project_id": owner["project_id"]}).json()["items"][0]
    assert row["source_id"] == row["target_id"] == item["id"]


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
@pytest.mark.parametrize("peer_count,status,relation_count", [(1, "self_excluded", 0), (2, "create", 2), (3, "ambiguous", 0)])
def test_preview_and_continuous_rules_exclude_self_before_counting_candidates(request, fixture_name, peer_count, status, relation_count):
    from ontology.worker import process_one

    client, app, owner, _ = request.getfixturevalue(fixture_name)
    release = publish(client, owner["project_id"], {"types": [{
        "key": "Peer", "label": "Peer",
        "properties": [{"key": "code", "label": "Code", "data_type": "string"}],
        "relations": [{"key": "peer", "label": "Peer", "target_type": "Peer", "cardinality": "one"}],
    }]})["release_id"]
    for _ in range(peer_count):
        response = client.post("/api/objects", json={
            "project_id": owner["project_id"], "type_key": "Peer", "properties": {"code": "same"},
        })
        assert response.status_code == 201, response.text
    rule = {
        "project_id": owner["project_id"], "expected_model_release_id": release,
        "source_type": "Peer", "relation_key": "peer", "source_field": "code",
        "target_field": "code", "allow_self": False,
    }
    plan = preview(client, rule)
    assert plan["counts"][status] == peer_count
    assert all(row["candidate_count"] == peer_count - 1 for row in plan["samples"])
    response = client.post("/api/relation-rules", json={"name": "Non-self peers", "rule": rule})
    assert response.status_code == 201, response.text
    for _ in range(20):
        if not process_one(app.state.db, app.state.settings):
            break
    else:
        raise AssertionError("Rule queue did not converge")
    rows = client.get("/api/relations", params={"project_id": owner["project_id"]}).json()["items"]
    assert len(rows) == relation_count
    assert all(row["source_id"] != row["target_id"] for row in rows)


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_concurrent_manual_and_matched_single_target_writes_have_one_winner(request, fixture_name):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    rule = setup_model(client, owner)
    source = obj(client, rule, "Item", "A")
    obj(client, rule, "Group", "A")
    other = obj(client, rule, "Group", "B")
    plan = preview(client, rule)
    barrier = Barrier(2)

    def submit(mode):
        with TestClient(app) as concurrent:
            concurrent.cookies.update(client.cookies)
            barrier.wait(timeout=10)
            if mode == "match":
                return apply(concurrent, rule, plan).status_code
            return concurrent.post(
                "/api/relations",
                json={
                    "project_id": owner["project_id"],
                    "type_key": "belongs_to",
                    "source_id": source["id"],
                    "target_id": other["id"],
                },
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, ["match", "manual"]))
    assert sorted(statuses) in ([200, 409], [201, 409]), statuses
    assert client.get("/api/relations", params={"project_id": owner["project_id"]}).json()["total"] == 1
