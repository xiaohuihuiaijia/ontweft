import pytest
from fastapi.testclient import TestClient
from ontology.core.errors import DomainError
from ontology.jobs.service import claim_for_tenant, finish
from ontology.worker import process_one

from tests.support.content import upload


def test_upload_worker_evidence_knowledge_and_published_revision(platform):
    client, app, a, _ = platform
    result = upload(client, a["project_id"])
    assert result["document"]["state"] == "pending" and result["job"]["payload"]["engine"] == "basic"
    assert process_one(app.state.db, app.state.settings)
    doc = client.get(f"/api/documents/{result['document']['id']}").json()
    assert doc["state"] == "ready" and doc["ir"]["complete"]
    reference = {
        "document_id": doc["id"],
        "revision": doc["revision"],
        "parse_id": doc["current_parse_id"],
        "parse_revision": doc["parse_revision"],
        "ir_fingerprint": doc["ir_fingerprint"],
        "content_hash": doc["content_hash"],
        "element_path": doc["ir"]["elements"][0]["evidence"]["element_path"],
    }
    base = {
        "project_id": a["project_id"],
        "title": "有出处的知识",
        "body": "经人工确认的内容",
        "references": [reference],
    }
    assert (
        client.post(
            "/api/knowledge", json={**base, "references": [{**reference, "revision": 99}]}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/knowledge", json={**base, "references": [{**reference, "element_path": "invented"}]}
        ).status_code
        == 422
    )
    page = client.post("/api/knowledge", json=base).json()
    assert (
        client.post(f"/api/knowledge/{page['id']}/publish", json={"expected_revision": 1}).status_code == 200
    )
    assert (
        client.patch(
            f"/api/knowledge/{page['id']}",
            json={
                "expected_revision": 1,
                "title": "尚未发布的新草稿",
                "body": "draft",
                "references": [reference],
            },
        ).status_code
        == 200
    )
    person = client.post(
        "/api/users",
        json={"email": "auditor@example.test", "display_name": "审计员", "password": "Auditor-Test-Password"},
    ).json()
    client.post(
        "/api/members", json={"project_id": a["project_id"], "user_id": person["id"], "role": "auditor"}
    )
    with TestClient(app) as auditor:
        auditor.post(
            "/api/auth/login",
            json={
                "tenant_slug": "alpha",
                "email": "auditor@example.test",
                "password": "Auditor-Test-Password",
            },
        )
        visible = auditor.get(f"/api/knowledge/{page['id']}").json()
        assert visible["title"] == base["title"] and visible["revision"] == 1
        assert auditor.get(f"/api/knowledge/{page['id']}/history").status_code == 403
    with TestClient(app) as other:
        other.post(
            "/api/auth/login",
            json={"tenant_slug": "beta", "email": "owner@example.com", "password": "Safe-Test-Password-2026"},
        )
        assert other.get(f"/api/documents/{doc['id']}/download").status_code == 404
    graph = client.get("/api/graph", params={"project_id": a["project_id"], "kind": "knowledge"}).json()
    assert any(edge["target"] == doc["id"] for edge in graph["edges"])


def test_knowledge_graph_reports_edge_truncation_and_preserves_baseline(platform, definition, monkeypatch):
    from ontology.knowledge import api as content_api

    from tests.support.platform import publish

    client, app, a, _ = platform
    # Exercise the actual API/DB with a smaller rendering budget. No graph or
    # authorization result is mocked, and the node budget remains unexhausted.
    monkeypatch.setattr(content_api, "KNOWLEDGE_GRAPH_EDGE_LIMIT", 2)
    publish(client, a["project_id"], definition)
    baseline = client.get("/api/ontology/definition", params={"project_id": a["project_id"]}).json()
    uploaded = upload(client, a["project_id"])
    assert process_one(app.state.db, app.state.settings)
    doc = client.get(f"/api/documents/{uploaded['document']['id']}").json()
    assert doc["state"] == "ready"
    for count in range(1, 4):
        response = client.post(
            "/api/knowledge",
            json={
                "project_id": a["project_id"],
                "title": f"Published knowledge {count}",
                "body": "Verified content",
                "references": [
                    {
                        "document_id": doc["id"],
                        "revision": doc["revision"],
                        "parse_id": doc["current_parse_id"],
                        "parse_revision": doc["parse_revision"],
                        "ir_fingerprint": doc["ir_fingerprint"],
                        "content_hash": doc["content_hash"],
                    }
                ],
            },
        )
        assert response.status_code == 201, response.text
        page = response.json()
        assert (
            client.post(f"/api/knowledge/{page['id']}/publish", json={"expected_revision": 1}).status_code
            == 200
        )
        response = client.get("/api/graph", params={"project_id": a["project_id"], "kind": "knowledge"})
        assert response.status_code == 200, response.text
        graph = response.json()
        assert graph["release_id"] == baseline["release_id"]
        assert graph["version"] == baseline["version"]
        assert len(graph["nodes"]) == count + 1
        assert len(graph["edges"]) == min(count, 2)
        assert graph["truncated"] is (count > 2)
        assert all(edge["target"] == doc["id"] and edge["kind"] == "citation" for edge in graph["edges"])


def test_cancelled_parser_cannot_commit_and_retry_restores_visible_state(platform):
    client, app, a, _ = platform
    result = upload(client, a["project_id"])
    old = claim_for_tenant(app.state.db, a["tenant_id"], "old-worker", 300)
    assert old and old["id"] == result["job"]["id"]
    assert client.post(f"/api/jobs/{old['id']}/cancel").status_code == 200
    assert client.get(f"/api/documents/{result['document']['id']}").json()["state"] == "cancelled"
    with pytest.raises(DomainError):
        finish(app.state.db, old, result={"invalid_late_result": True})
    assert client.post(f"/api/jobs/{old['id']}/retry").status_code == 200
    assert client.get(f"/api/documents/{result['document']['id']}").json()["state"] == "pending"
    assert process_one(app.state.db, app.state.settings)
    assert client.get(f"/api/documents/{result['document']['id']}").json()["state"] == "ready"


def test_queued_parse_rechecks_current_project_permission(platform):
    client, app, a, _ = platform
    person = client.post(
        "/api/users",
        json={
            "email": "operator@example.test",
            "display_name": "操作员",
            "password": "Operator-Test-Password",
        },
    ).json()
    client.post(
        "/api/members", json={"project_id": a["project_id"], "user_id": person["id"], "role": "operator"}
    )
    with TestClient(app) as operator:
        operator.post(
            "/api/auth/login",
            json={
                "tenant_slug": "alpha",
                "email": "operator@example.test",
                "password": "Operator-Test-Password",
            },
        )
        result = upload(operator, a["project_id"])
    client.post(
        "/api/members", json={"project_id": a["project_id"], "user_id": person["id"], "role": "auditor"}
    )
    assert process_one(app.state.db, app.state.settings)
    doc = client.get(f"/api/documents/{result['document']['id']}").json()
    assert doc["state"] == "failed" and doc["ir"] == {}
    assert "权限" in doc["error"]


def test_default_word_upload_uses_native_parser_without_optional_docling(platform):
    from tests.support.documents import word

    client, app, a, _ = platform
    data = word("<w:p><w:r><w:t>阀门每季度检查一次。</w:t></w:r></w:p>")
    result = upload(client, a["project_id"], data, "维修手册.docx")
    assert result["job"]["payload"]["engine"] == "basic"
    assert process_one(app.state.db, app.state.settings)
    parsed = client.get(f"/api/documents/{result['document']['id']}").json()
    assert parsed["state"] == "ready", parsed.get("error")
    assert parsed["ir"]["parser"] == "basic/docx"
    assert any("阀门" in e["text"] for e in parsed["ir"]["elements"])
