import copy

import pytest
from ontology.core.domain import active_definition
from ontology.core.errors import DomainError
from ontology.identity.models import User
from ontology.identity.security import access
from ontology.intelligence.connection_api import router
from ontology.intelligence.projection import import_proposal, prepare_document_source
from ontology.intelligence.proposal_models import AIProposal, DocumentRelationContribution
from ontology.modeling.models import ChangeSet
from ontology.modeling.schemas import Definition
from ontology.objects.models import Entity, Relation, RelationRevision
from ontology.sources.ingestion import fingerprint
from ontology.sources.models import Source, SourceRecordRevision, SourceSnapshot
from sqlalchemy import func, select
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - fixture registration


@pytest.fixture(params=["platform", "pg_platform"])
def document_case(request):
    client, app, a, _b = request.getfixturevalue(request.param)
    if not any(getattr(route, "path", "") == "/api/model-connections" for route in app.routes):
        app.include_router(router, prefix="/api")
    connection = client.post("/api/model-connections", json={"name": "test", "base_url": "https://example.test/v1"}).json()
    document = client.post("/api/documents/upload", data={"project_id": a["project_id"]}, files={
        "file": ("public-fixture.txt", b"A has rating 12. B belongs to A.", "text/plain")
    }).json()["document"]
    definition = {"types": [{
        "key": "Product", "label": "Product", "properties": [
            {"key": "code", "label": "Code", "data_type": "string", "required": True},
            {"key": "rating", "label": "Rating", "data_type": "integer"},
        ], "unique_keys": [["code"]],
        "relations": [{"key": "parent", "label": "Parent", "target_type": "Product", "cardinality": "one"}],
    }], "mappings": []}
    evidence = [{"document_id": document["id"], "page": 1, "quote": "A has rating 12."}]
    candidates = [{"id": "a", "type_key": "Product", "properties": {"code": "A", "rating": 12},
                   "business_key": {"code": "A"}, "selected": True, "evidence": evidence}]
    return client, app, a, connection, document, definition, candidates


def prepare(case, *, candidates=None, definition=None, relations=None):
    _, app, ids, connection, document, base, initial = case
    with app.state.db.transaction(ids["tenant_id"], write=True) as session:
        user = session.get(User, ids["user_id"])
        ctx = access(session, user, ids["project_id"], "ontology:write")
        proposal = AIProposal(
            tenant_id=ids["tenant_id"], project_id=ids["project_id"], owner_id=user.id,
            connection_id=connection["id"], connection_revision=1,
            baseline_release_id=ctx.project.active_release_id, inputs={"documents": [{"id": document["id"]}]},
            status="adopted", definition=copy.deepcopy(definition or base), candidates=copy.deepcopy(candidates or initial),
            relations=copy.deepcopy(relations or []),
        )
        session.add(proposal)
        session.flush()
        prepare_document_source(session, proposal, user)
        if Definition.model_validate(proposal.definition) != Definition.model_validate(active_definition(session, ctx.project)):
            change = ChangeSet(
                tenant_id=ids["tenant_id"], project_id=ids["project_id"], author_id=user.id,
                title="Reviewed document mapping", definition=proposal.definition,
                baseline_release_id=ctx.project.active_release_id,
            )
            session.add(change)
            session.flush()
            proposal.changeset_id = change.id
        return proposal.id, proposal.definition, proposal.source_id


def publish_prepared(case, proposal_id):
    client, app, ids, *_ = case
    with app.state.db.transaction(ids["tenant_id"]) as session:
        change_id = session.get(AIProposal, proposal_id).changeset_id
    change = client.get(f"/api/changesets/{change_id}").json()
    for operation in ("validate", "approve", "publish"):
        response = client.post(
            f"/api/changesets/{change_id}/{operation}", json={"expected_revision": change["revision"]}
        )
        assert response.status_code == 200, response.text
        change = response.json()


def apply(case, proposal_id):
    _, app, ids, *_ = case
    with app.state.db.transaction(ids["tenant_id"], write=True) as session:
        user = session.get(User, ids["user_id"])
        ctx = access(session, user, ids["project_id"], "objects:write")
        return import_proposal(session, session.get(AIProposal, proposal_id), ctx)


def test_approved_document_facts_use_contributions_and_idempotent_marker(document_case):
    _client, app, ids, *_ = document_case
    proposal_id, _definition, source_id = prepare(document_case)
    publish_prepared(document_case, proposal_id)
    first = apply(document_case, proposal_id)
    assert first["imported"] == 1 and first["counts"]["created"] == 1
    assert apply(document_case, proposal_id) == first
    with app.state.db.transaction(ids["tenant_id"]) as session:
        assert session.scalar(select(func.count()).select_from(Entity)) == 1
        assert session.get(Source, source_id).kind == "document"
        record = session.scalar(select(SourceRecordRevision))
        assert record.record_codec == "snapshot-json-v1"
        assert record.values["__ontweft_field_evidence"]["rating"][0]["quote"] == "A has rating 12."
        assert record.content_hash == fingerprint({"key": record.source_key, "identity_key": record.identity_key, "values": record.values})
        assert session.get(SourceSnapshot, record.snapshot_id).status == "complete"
        proposal = session.get(AIProposal, proposal_id)
        assert proposal.status == "imported" and proposal.revision == 2


def test_narrower_second_goal_preserves_existing_fields_and_business_identity(document_case):
    _client, app, ids, *_ = document_case
    first_id, definition, source_id = prepare(document_case)
    publish_prepared(document_case, first_id)
    initial_result = apply(document_case, first_id)
    assert initial_result["counts"]["blocked"] == 0, initial_result
    candidate = copy.deepcopy(document_case[-1][0])
    candidate["properties"] = {"code": "A"}
    candidate["evidence"][0]["quote"] = "B belongs to A."
    second_id, second_definition, second_source = prepare(document_case, definition=definition, candidates=[candidate])
    assert second_source == source_id
    assert second_definition == definition
    assert apply(document_case, second_id)["counts"]["created"] == 0
    with app.state.db.transaction(ids["tenant_id"]) as session:
        entity = session.scalar(select(Entity))
        assert entity.properties == {"code": "A", "rating": 12}
        assert session.scalar(select(func.count()).select_from(Entity)) == 1


def test_unpublished_mapping_cannot_materialize_candidates(document_case):
    proposal_id, _, _ = prepare(document_case)
    with pytest.raises(DomainError) as caught:
        apply(document_case, proposal_id)
    assert caught.value.code == "revision_conflict"
    assert "请先发布" in caught.value.message


def test_missing_business_key_is_not_replaced_with_row_number(document_case):
    candidate = copy.deepcopy(document_case[-1][0])
    candidate["business_key"] = {}
    with pytest.raises(DomainError) as caught:
        prepare(document_case, candidates=[candidate])
    assert caught.value.code == "candidate_identity_required"


def test_unique_business_key_must_match_candidate_properties(document_case):
    candidate = copy.deepcopy(document_case[-1][0])
    candidate["business_key"] = {"code": "B"}
    with pytest.raises(DomainError) as caught:
        prepare(document_case, candidates=[candidate])
    assert caught.value.code == "candidate_identity_mismatch"


def test_document_relation_coexists_with_manual_relation(document_case):
    client, app, ids, *_ = document_case
    candidates = copy.deepcopy(document_case[-1])
    second = copy.deepcopy(candidates[0])
    second.update(id="b", properties={"code": "B"}, business_key={"code": "B"})
    candidates.append(second)
    first_id, definition, _ = prepare(document_case, candidates=candidates)
    publish_prepared(document_case, first_id)
    apply(document_case, first_id)
    with app.state.db.transaction(ids["tenant_id"], write=True) as session:
        entities = {row.properties["code"]: row for row in session.scalars(select(Entity)).all()}
        manual = Relation(
            tenant_id=ids["tenant_id"], project_id=ids["project_id"], source_id=entities["B"].id,
            target_id=entities["A"].id, type_key="parent", properties={}, evidence=[], manual=True,
        )
        session.add(manual)
        session.flush()
        relation_id = manual.id
    relation = {"id": "r", "source_candidate_id": "b", "target_candidate_id": "a", "key": "parent", "selected": True, "evidence": candidates[0]["evidence"]}
    second_id, _, _ = prepare(document_case, definition=definition, candidates=candidates, relations=[relation])
    assert apply(document_case, second_id)["relations"] == 1
    with app.state.db.transaction(ids["tenant_id"]) as session:
        assert session.get(Relation, relation_id).manual
        assert session.scalar(select(func.count()).select_from(Relation)) == 1
        assert session.scalar(select(DocumentRelationContribution)).relation_id == relation_id
        revision = session.get(Relation, relation_id).revision
    result = client.request("DELETE", f"/api/relations/{relation_id}", json={"expected_revision": revision})
    assert result.status_code == 200, result.text
    assert result.json()["retained_by_document"]
    with app.state.db.transaction(ids["tenant_id"]) as session:
        assert session.get(Relation, relation_id) is not None
        assert not session.get(Relation, relation_id).manual


def test_relation_conditions_preserve_conflicts_and_do_not_roll_back_objects(document_case):
    _client, app, ids, *_ = document_case
    definition = copy.deepcopy(document_case[-2])
    definition["types"][0]["relations"][0]["properties"] = [
        {"key": "limit", "label": "Voltage limit", "data_type": "number", "unit": "V"},
        {"key": "condition", "label": "Condition", "data_type": "string"},
    ]
    candidates = copy.deepcopy(document_case[-1])
    candidates.append({**copy.deepcopy(candidates[0]), "id": "b", "properties": {"code": "B"}, "business_key": {"code": "B"}})
    relation = {"id": "r", "source_candidate_id": "b", "target_candidate_id": "a", "key": "parent",
                "selected": True, "properties": {"limit": "12.50"}, "evidence": candidates[0]["evidence"]}
    first_id, definition, _ = prepare(document_case, definition=definition, candidates=candidates, relations=[relation])
    publish_prepared(document_case, first_id)
    assert apply(document_case, first_id)["relations"] == 1
    with app.state.db.transaction(ids["tenant_id"]) as session:
        row = session.scalar(select(Relation))
        relation_id, first_properties = row.id, row.properties
        assert row.revision == 1
        assert session.scalar(select(func.count()).select_from(RelationRevision)) == 1
    # Decimal spellings are equivalent; do not create a conflict or a redundant revision.
    same_id, _, _ = prepare(document_case, definition=definition, candidates=candidates,
                            relations=[{**relation, "properties": {"limit": "12.500"}}])
    assert apply(document_case, same_id)["relations_blocked"] == 0
    fill_evidence = [{**relation["evidence"][0], "quote": "B belongs to A."}]
    fill_id, _, _ = prepare(document_case, definition=definition, candidates=candidates,
                            relations=[{**relation, "properties": {"condition": "dry installation"}, "evidence": fill_evidence}])
    assert apply(document_case, fill_id)["relations"] == 1
    with app.state.db.transaction(ids["tenant_id"]) as session:
        row = session.get(Relation, relation_id)
        assert row.properties == {**first_properties, "condition": "dry installation"}
        assert row.revision == 2
        assert session.scalar(select(func.count()).select_from(RelationRevision)) == 2
        assert fill_evidence[0] in row.evidence
        latest = session.scalar(select(RelationRevision).where(RelationRevision.revision == 2))
        assert fill_evidence[0] in latest.evidence
        contribution = session.scalar(select(DocumentRelationContribution))
        assert fill_evidence[0] in contribution.evidence
    # A valid new object still lands when a relation's proposed condition conflicts.
    candidates.append({**copy.deepcopy(candidates[0]), "id": "c", "properties": {"code": "C"}, "business_key": {"code": "C"}})
    conflict_id, _, _ = prepare(document_case, definition=definition, candidates=candidates,
                                relations=[{**relation, "properties": {"limit": "24"}}])
    result = apply(document_case, conflict_id)
    assert result["counts"]["created"] == 1 and result["imported"] == 3
    assert result["relations"] == 0 and result["relations_blocked"] == 1
    assert result["issues"][-1]["code"] == "relation_property_conflict"
    assert result["issues"][-1]["relation_id"] == relation_id
    assert apply(document_case, conflict_id) == result
    with app.state.db.transaction(ids["tenant_id"]) as session:
        row = session.get(Relation, relation_id)
        assert row.revision == 2 and row.properties["limit"] == first_properties["limit"]
        proposal = session.get(AIProposal, conflict_id)
        assert proposal.imported == result and proposal.relations[0]["properties"] == {"limit": "24"}
        assert session.scalar(select(func.count()).select_from(Entity)) == 3


def test_relation_cardinality_conflict_keeps_successful_object_import(document_case):
    _client, app, ids, *_ = document_case
    candidates = copy.deepcopy(document_case[-1])
    for code in ("B", "C"):
        candidates.append({**copy.deepcopy(candidates[0]), "id": code.lower(), "properties": {"code": code}, "business_key": {"code": code}})
    relation = {"id": "r", "source_candidate_id": "b", "target_candidate_id": "a", "key": "parent",
                "selected": True, "evidence": candidates[0]["evidence"]}
    first_id, definition, _ = prepare(document_case, candidates=candidates, relations=[relation])
    publish_prepared(document_case, first_id)
    apply(document_case, first_id)
    next_id, _, _ = prepare(document_case, definition=definition, candidates=candidates,
                            relations=[{**relation, "target_candidate_id": "c"}])
    result = apply(document_case, next_id)
    assert result["imported"] == 3 and result["relations_blocked"] == 1
    assert result["issues"][-1]["code"] == "relation_cardinality_conflict"
    with app.state.db.transaction(ids["tenant_id"]) as session:
        assert session.scalar(select(func.count()).select_from(Relation)) == 1
