import pytest
from ontology.identity.models import Membership
from ontology.objects.models import Relation
from ontology.objects.rule_models import RelationContribution, RelationRule, RelationWork
from ontology.worker import process_one
from sqlalchemy import select

from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_continuous_matching_preserves_manual_support_and_parks_revoked_rules(request, fixture_name):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    model = {
        "types": [
            {
                "key": "Item",
                "label": "Item",
                "properties": [{"key": "code", "label": "Code", "data_type": "string"}],
                "relations": [
                    {"key": "belongs", "label": "Belongs", "target_type": "Group", "cardinality": "many"}
                ],
            },
            {
                "key": "Group",
                "label": "Group",
                "properties": [{"key": "code", "label": "Code", "data_type": "string"}],
            },
        ]
    }
    release = publish(client, owner["project_id"], model)

    def obj(kind, code):
        result = client.post(
            "/api/objects",
            json={"project_id": owner["project_id"], "type_key": kind, "properties": {"code": code}},
        )
        assert result.status_code == 201, result.text
        return result.json()

    a, b, c = obj("Item", "A"), obj("Group", "A"), obj("Group", "B")
    rule = {
        "project_id": owner["project_id"],
        "expected_model_release_id": release["release_id"],
        "source_type": "Item",
        "relation_key": "belongs",
        "source_field": "code",
        "target_field": "code",
    }
    result = client.post("/api/relation-rules", json={"name": "Automatic belonging", "rule": rule})
    assert result.status_code == 201, result.text

    def drain():
        for _ in range(20):
            if not process_one(app.state.db, app.state.settings):
                break
        else:
            raise AssertionError("Rule queue did not converge")

    def rels():
        return client.get("/api/relations", params={"project_id": owner["project_id"]}).json()["items"]

    drain()
    assert len(rels()) == 1 and rels()[0]["target_id"] == b["id"] and not rels()[0]["manual"]
    # Retaining a generated relation must honor the same input contract as creation.
    for invalid in (
        {"properties": {"unsupported": True}},
        {"evidence": [{"document_id": "missing", "revision": 1}]},
    ):
        rejected = client.post(
            "/api/relations",
            json={
                "project_id": owner["project_id"],
                "source_id": a["id"],
                "target_id": b["id"],
                "type_key": "belongs",
                **invalid,
            },
        )
        assert rejected.status_code in {404, 422}, rejected.text
        assert not rels()[0]["manual"]
    # Explicit manual support of the generated relation survives rematching.
    assert (
        client.post(
            "/api/relations",
            json={
                "project_id": owner["project_id"],
                "source_id": a["id"],
                "target_id": b["id"],
                "type_key": "belongs",
            },
        ).status_code
        == 201
    )
    a = client.patch(
        "/api/objects/" + a["id"], json={"expected_revision": a["revision"], "properties": {"code": "B"}}
    ).json()
    drain()
    assert {r["target_id"] for r in rels()} == {b["id"], c["id"]}
    # Ambiguous changes retain the old owned relation and mark only that record.
    obj("Group", "B")
    drain()
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(RelationWork)).status == "blocked"
        assert len(session.scalars(select(RelationContribution)).all()) == 1
        assert len(session.scalars(select(Relation)).all()) == 2
        membership = session.scalar(select(Membership).where(Membership.project_id == owner["project_id"]))
        membership.role = "viewer"
        configured = session.scalar(select(RelationRule))
        from ontology.objects.relation_rules import enqueue_rule

        enqueue_rule(session, configured)
    drain()
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert not session.scalar(select(RelationRule)).enabled
        assert len(session.scalars(select(Relation)).all()) == 2
