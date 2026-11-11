import copy

import pytest
from ontology.core.errors import DomainError
from ontology.identity.models import User
from ontology.identity.security import access
from ontology.intelligence.connection_api import router
from ontology.intelligence.projection import import_proposal, prepare_document_source
from ontology.intelligence.proposal_models import AIProposal, DocumentRelationContribution
from ontology.objects.models import Entity, Relation
from ontology.sources.ingestion import fingerprint
from ontology.sources.models import Source, SourceRecordRevision, SourceSnapshot
from sqlalchemy import func, select
from tests.support.platform import publish
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
            status="ready", definition=copy.deepcopy(definition or base), candidates=copy.deepcopy(candidates or initial),
            relations=copy.deepcopy(relations or []),
        )
        session.add(proposal)
        session.flush()
        prepare_document_source(session, proposal, user)
        return proposal.id, proposal.definition, proposal.source_id


def apply(case, proposal_id):
    _, app, ids, *_ = case
    with app.state.db.transaction(ids["tenant_id"], write=True) as session:
        user = session.get(User, ids["user_id"])
        ctx = access(session, user, ids["project_id"], "objects:write")
        return import_proposal(session, session.get(AIProposal, proposal_id), ctx)


def test_approved_document_facts_use_contributions_and_idempotent_marker(document_case):
    client, app, ids, *_ = document_case
    proposal_id, definition, source_id = prepare(document_case)
    publish(client, ids["project_id"], definition)
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
    client, app, ids, *_ = document_case
    first_id, definition, source_id = prepare(document_case)
    publish(client, ids["project_id"], definition)
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
    assert caught.value.code == "document_mapping_unpublished"


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
    publish(client, ids["project_id"], definition)
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
    result = client.request("DELETE", f"/api/relations/{relation_id}", json={"expected_revision": 1})
    assert result.status_code == 200, result.text
    assert result.json()["retained_by_document"]
    with app.state.db.transaction(ids["tenant_id"]) as session:
        assert session.get(Relation, relation_id) is not None
        assert not session.get(Relation, relation_id).manual
