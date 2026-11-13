"""Relation attributes are confirmed values; rules continue to maintain existence only."""

from copy import deepcopy

from ontology.identity.models import Membership
from ontology.objects.models import Entity, Relation
from ontology.worker import process_one
from sqlalchemy import select

from tests.support.platform import publish


def relation_case(platform, definition):
    client, app, owner, _ = platform
    definition = deepcopy(definition)
    definition["types"][0]["relations"][0]["properties"] = [
        {"key": "condition", "label": "适用条件", "data_type": "string"},
        {"key": "rating", "label": "额定值", "data_type": "number"},
        {
            "key": "internal",
            "label": "内部说明",
            "data_type": "string",
            "read_roles": ["owner"],
            "write_roles": ["owner"],
        },
    ]
    release = publish(client, owner["project_id"], definition)["release_id"]
    endpoints = []
    for name in ("source", "target"):
        response = client.post(
            "/api/objects",
            json={
                "project_id": owner["project_id"],
                "type_key": "Project",
                "properties": {"name": name},
                "expected_model_release_id": release,
            },
        )
        assert response.status_code == 201, response.text
        endpoints.append(response.json())
    payload = {
        "project_id": owner["project_id"],
        "type_key": "parent",
        "source_id": endpoints[0]["id"],
        "target_id": endpoints[1]["id"],
        "expected_model_release_id": release,
        "properties": {"condition": "dry environment", "rating": "12.50", "internal": "owner only"},
    }
    response = client.post("/api/relations", json=payload)
    assert response.status_code == 201, response.text
    return {
        "client": client,
        "app": app,
        "owner": owner,
        "release": release,
        "endpoints": endpoints,
        "relation": response.json(),
        "payload": payload,
        "definition": definition,
    }


def edit(case, **values):
    return case["client"].patch(
        f"/api/relations/{case['relation']['id']}",
        json={
            "expected_revision": case["relation"]["revision"],
            "expected_model_release_id": case["release"],
            **values,
        },
    )


def test_partial_edits_keep_values_and_deleted_history(platform, definition):
    case = relation_case(platform, definition)
    client, relation = case["client"], case["relation"]
    with case["app"].state.db.transaction(case["owner"]["tenant_id"]) as session:
        previous_sequence = session.get(Entity, relation["source_id"]).changed_seq
    changed = edit(case, properties={"rating": None}, reason="规格暂缺，保留其他字段")
    assert changed.status_code == 200, changed.text
    current = changed.json()
    assert current["properties"] == {"rating": None, "condition": "dry environment", "internal": "owner only"}
    assert current["revision"] == relation["revision"] + 1
    assert current["definition"]["properties"][0]["key"] == "condition"
    assert edit(case, properties={"condition": "stale"}).status_code == 409
    case["relation"] = current
    removed = edit(case, remove_properties=["condition"])
    assert removed.status_code == 200, removed.text
    assert "condition" not in removed.json()["properties"]
    assert "rating" in removed.json()["properties"]
    case["relation"] = removed.json()
    assert edit(case, properties={"rating": "bad"}).status_code == 422
    assert edit(case, properties={"not_defined": 1}).status_code == 422
    assert edit(case, properties={"rating": "2"}, remove_properties=["rating"]).status_code == 422
    with case["app"].state.db.transaction(case["owner"]["tenant_id"]) as session:
        assert session.get(Entity, relation["source_id"]).changed_seq > previous_sequence
    result = client.request(
        "DELETE",
        f"/api/relations/{relation['id']}",
        json={"expected_revision": removed.json()["revision"], "expected_model_release_id": case["release"]},
    )
    assert result.status_code == 200, result.text
    assert client.get(f"/api/relations/{relation['id']}").status_code == 404
    history = client.get(f"/api/relations/{relation['id']}/history", params={"limit": 2}).json()
    assert history["total"] == 4
    assert [row["operation"] for row in history["items"]] == ["deleted", "updated"]
    original = client.get(f"/api/relations/{relation['id']}/history", params={"offset": 3}).json()["items"][0]
    assert original["properties"]["rating"] == "12.50"
    assert original["model_release_id"] == case["release"]


def test_read_and_write_roles_apply_to_detail_list_and_history(platform, definition):
    case = relation_case(platform, definition)
    client, app, owner = case["client"], case["app"], case["owner"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        membership = session.scalar(select(Membership).where(Membership.project_id == owner["project_id"]))
        membership.role = "operator"
    relation_id = case["relation"]["id"]
    for row in (
        client.get(f"/api/relations/{relation_id}").json(),
        client.get("/api/relations", params={"project_id": owner["project_id"]}).json()["items"][0],
        client.get(f"/api/relations/{relation_id}/history").json()["items"][0],
    ):
        assert "internal" not in row["properties"]
        assert "internal" not in {prop["key"] for prop in row["definition"]["properties"]}
    assert edit(case, properties={"internal": "guess"}).status_code == 403
    assert edit(case, remove_properties=["internal"]).status_code == 403
    updated = edit(case, properties={"condition": "outdoors"})
    assert updated.status_code == 200, updated.text
    assert "internal" not in updated.json()["properties"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.get(Relation, relation_id).properties["internal"] == "owner only"


def test_repeat_create_preserves_confirmed_values_and_checks_current_model(platform, definition):
    case = relation_case(platform, definition)
    payload = {**case["payload"], "properties": {"rating": "12.500"}}
    repeat = case["client"].post("/api/relations", json=payload)
    assert repeat.status_code == 201, repeat.text
    assert repeat.json()["id"] == case["relation"]["id"]
    payload["properties"] = {"rating": "13"}
    assert case["client"].post("/api/relations", json=payload).status_code == 409
    renamed = deepcopy(case["definition"])
    renamed["types"][0]["relations"][0]["label"] = "Updated label"
    published = publish(case["client"], case["owner"]["project_id"], renamed)["release_id"]
    assert edit(case, properties={"condition": "new"}).status_code == 409
    detail = case["client"].get(f"/api/relations/{case['relation']['id']}").json()
    assert detail["definition"]["label"] == "Updated label"
    assert detail["model_release_id"] == published
    history = case["client"].get(f"/api/relations/{case['relation']['id']}/history").json()
    assert all(row["model_release_id"] == case["release"] for row in history["items"])


def test_rules_preserve_attributes_and_record_last_support_removal(platform):
    client, app, owner, _ = platform
    definition = {
        "types": [
            {
                "key": "Item",
                "label": "Item",
                "properties": [{"key": "code", "label": "Code", "data_type": "string"}],
                "relations": [
                    {
                        "key": "belongs",
                        "label": "Belongs",
                        "target_type": "Group",
                        "cardinality": "one",
                        "properties": [{"key": "condition", "label": "Condition", "data_type": "string"}],
                    }
                ],
            },
            {
                "key": "Group",
                "label": "Group",
                "properties": [{"key": "code", "label": "Code", "data_type": "string"}],
            },
        ]
    }
    release = publish(client, owner["project_id"], definition)["release_id"]
    endpoints = []
    for kind in ("Item", "Group"):
        response = client.post(
            "/api/objects",
            json={"project_id": owner["project_id"], "type_key": kind, "properties": {"code": "A"}},
        )
        assert response.status_code == 201, response.text
        endpoints.append(response.json())
    response = client.post(
        "/api/relation-rules",
        json={
            "name": "By code",
            "rule": {
                "project_id": owner["project_id"],
                "expected_model_release_id": release,
                "source_type": "Item",
                "relation_key": "belongs",
                "source_field": "code",
                "target_field": "code",
            },
        },
    )
    assert response.status_code == 201, response.text

    def drain():
        for _ in range(20):
            if not process_one(app.state.db, app.state.settings):
                return
        raise AssertionError("Rule work did not finish")

    drain()
    relation = client.get("/api/relations", params={"project_id": owner["project_id"]}).json()["items"][0]
    response = client.patch(
        f"/api/relations/{relation['id']}",
        json={
            "expected_revision": relation["revision"],
            "expected_model_release_id": release,
            "properties": {"condition": "Reviewed condition"},
        },
    )
    assert response.status_code == 200, response.text
    assert not response.json()["manual"]
    drain()
    assert client.get(f"/api/relations/{relation['id']}").json()["properties"] == {
        "condition": "Reviewed condition"
    }
    # Removing the matching source key removes the edge, but does not erase its confirmed history.
    response = client.patch(
        f"/api/objects/{endpoints[0]['id']}",
        json={"expected_revision": endpoints[0]["revision"], "properties": {"code": "B"}},
    )
    assert response.status_code == 200, response.text
    drain()
    assert client.get(f"/api/relations/{relation['id']}").status_code == 404
    history = client.get(f"/api/relations/{relation['id']}/history").json()["items"]
    assert history[0]["operation"] == "deleted"
    assert history[0]["properties"] == {"condition": "Reviewed condition"}
    assert history[-1]["operation"] == "created"


def test_relation_reads_merge_all_contributions_and_redact_evidence(platform, definition):
    from ontology.documents.storage_models import Document
    from ontology.intelligence.models import ModelConnection
    from ontology.intelligence.proposal_models import AIProposal, DocumentRelationContribution
    from ontology.sources.models import Source

    case = relation_case(platform, definition)
    client, app, owner, other = platform
    with app.state.db.transaction(other["tenant_id"]) as session:
        hidden = Document(
            tenant_id=other["tenant_id"],
            project_id=other["project_id"],
            filename="other.txt",
            content_hash="0" * 64,
            storage_key="not-read",
            size=1,
            uploader_id=other["user_id"],
        )
        session.add(hidden)
        session.flush()
        foreign_document = hidden.id
    with app.state.db.transaction(owner["tenant_id"]) as session:
        connection = ModelConnection(
            tenant_id=owner["tenant_id"], name="Synthetic", base_url="https://example.test/v1"
        )
        session.add(connection)
        session.flush()
        proposal = AIProposal(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            owner_id=owner["user_id"],
            connection_id=connection.id,
            connection_revision=1,
            inputs={},
        )
        session.add(proposal)
        session.flush()
        references = []
        for number in (1, 2):
            document = Document(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                filename=f"public-{number}.txt",
                content_hash=str(number) * 64,
                storage_key="not-read",
                size=1,
                uploader_id=owner["user_id"],
            )
            source = Source(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                name=f"evidence {number}",
                kind="document",
            )
            session.add_all([document, source])
            session.flush()
            ref = {"document_id": document.id, "page": number, "quote": f"evidence {number}"}
            references.append(ref)
            session.add(
                DocumentRelationContribution(
                    tenant_id=owner["tenant_id"],
                    project_id=owner["project_id"],
                    source_id=source.id,
                    proposal_id=proposal.id,
                    relation_id=case["relation"]["id"],
                    fact_key=str(number) * 64,
                    evidence=[ref],
                )
            )
        relation = session.get(Relation, case["relation"]["id"])
        relation.evidence = [
            references[0],
            {**references[0], "quote": "restricted field", "fields": ["internal"]},
            {"document_id": foreign_document, "quote": "other tenant private text"},
        ]
    detail = client.get(f"/api/relations/{case['relation']['id']}").json()
    assert {ref["quote"] for ref in detail["evidence"]} == {"evidence 1", "evidence 2", "restricted field"}
    # Snapshot an edit and then restrict a field: both current and historical evidence use current permissions.
    assert edit(case, properties={"condition": "confirmed"}).status_code == 200
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.scalar(
            select(Membership).where(Membership.project_id == owner["project_id"])
        ).role = "operator"
    detail = client.get(f"/api/relations/{case['relation']['id']}").json()
    listing = client.get("/api/relations", params={"project_id": owner["project_id"]}).json()["items"][0]
    for row in (detail, listing):
        assert {ref["quote"] for ref in row["evidence"]} == {"evidence 1", "evidence 2"}
    history = client.get(f"/api/relations/{case['relation']['id']}/history").json()["items"]
    assert all(
        ref["quote"] not in {"restricted field", "other tenant private text"}
        for row in history
        for ref in row["evidence"]
    )


def test_explicit_evidence_confirmation_requires_real_page_and_quote(platform, definition):
    from ontology.documents.storage_models import Document
    from ontology.intelligence.proposal_models import AIPage

    case = relation_case(platform, definition)
    owner = case["owner"]
    with case["app"].state.db.transaction(owner["tenant_id"]) as session:
        document = Document(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            filename="synthetic.txt",
            content_hash="3" * 64,
            storage_key="not-read",
            size=1,
            uploader_id=owner["user_id"],
        )
        session.add(document)
        session.flush()
        page = AIPage(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            document_id=document.id,
            source_revision=1,
            page_number=1,
            cache_key="4" * 64,
            model="synthetic",
            result={"text": "Use indoors in a dry environment."},
        )
        session.add(page)
        session.flush()
        reference = {
            "document_id": document.id,
            "page": 1,
            "parse_id": page.id,
            "native_parse_id": None,
            "quote": "Use indoors",
        }
    for changes in ({"quote": "Works under water"}, {"page": 2}, {"native_parse_id": "fabricated"}):
        response = edit(case, evidence=[{**reference, **changes}])
        assert response.status_code == 422, response.text
    response = edit(case, evidence=[reference], reason="核对来源后补充依据")
    assert response.status_code == 200, response.text
    assert response.json()["evidence"] == [reference]
    case["relation"] = response.json()
    repeat = edit(case, evidence=[reference])
    assert repeat.status_code == 200
    assert repeat.json()["revision"] == response.json()["revision"]


def test_graph_projects_fields_without_loading_discarded_evidence(platform, definition, monkeypatch):
    from ontology.content_access import evidence

    case = relation_case(platform, definition)
    with case["app"].state.db.transaction(case["owner"]["tenant_id"]) as session:
        source = session.get(Entity, case["endpoints"][0]["id"])
        source.evidence = [{"document_id": "unneeded-document", "quote": "not returned by graph"}]
        source.properties = {**source.properties, "budget": "100"}
        session.scalar(
            select(Membership).where(Membership.project_id == case["owner"]["project_id"])
        ).role = "operator"

    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("Canvas payload does not use document evidence")

    monkeypatch.setattr(evidence, "filter_evidence", unexpected_lookup)
    response = case["client"].get("/api/graph", params={"project_id": case["owner"]["project_id"]})
    assert response.status_code == 200, response.text
    graph = response.json()
    assert graph["edges"][0]["revision"] == case["relation"]["revision"]
    assert all("budget" not in node["properties"] and "evidence" not in node for node in graph["nodes"])
