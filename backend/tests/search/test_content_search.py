"""Content retrieval through real transactions, published versions and evidence."""

import copy

import ontology.search.content.index as content_search_index
from fastapi.testclient import TestClient
from ontology.documents.storage_models import Document
from ontology.identity.models import Membership, Project, Tenant
from ontology.jobs.models import Job
from ontology.search.content.index import enqueue_content, process_one_content_index
from ontology.search.content.models import ContentSearchChunk, ContentSearchDocument
from ontology.search.content.units import ir_fingerprint
from ontology.worker import process_one, run_worker
from sqlalchemy import delete, func, select

from tests.support.content import upload
from tests.support.content_search import content_platform as content_platform  # noqa: PLC0414 - pytest fixture registration
from tests.support.content_search import drain, parsed_doc, published_page, resolve, search
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def test_body_chinese_and_exact_download_without_model_release(content_platform):
    client, app, owner, _ = content_platform
    doc = parsed_doc(client, app, owner)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.get(Project, owner["project_id"]).active_release_id is None
    assert search(client, owner, "长江大桥 AI").json()["total"] == 0
    drain(app, owner)
    response = search(client, owner, "南京 长江大桥 AI")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["total"] == 1 and result["query"]["mode"] == "all_terms_in_fragment"
    hit = result["items"][0]
    assert hit["asset_id"] == doc["id"] and hit["parse_revision"] == 1
    evidence = resolve(client, owner, hit)
    assert evidence.status_code == 200, evidence.text
    data = evidence.json()
    assert data["unit"]["chunk_text"] == hit["excerpt"]
    assert data["unit"]["evidence"]["content_hash"] == doc["content_hash"]
    download = client.get(data["download_url"])
    assert download.status_code == 200 and "南京" in download.text
    assert (
        client.get(data["download_url"].replace("expected_revision=1", "expected_revision=2")).status_code
        == 409
    )
    assert client.get(f"/api/documents/{doc['id']}/download?expected_revision=1").status_code == 422
    assert search(client, owner, "!!!").status_code == 422


def test_published_knowledge_stays_on_published_body_and_mode_is_server_enforced(content_platform):
    client, app, owner, _ = content_platform
    page = published_page(client, owner)
    draft = client.post(
        "/api/knowledge",
        json={
            "project_id": owner["project_id"],
            "title": "机密草稿",
            "body": "未发布机密",
            "references": [],
        },
    ).json()
    drain(app, owner)
    hit = search(client, owner, "阀门").json()["items"][0]
    assert hit["kind"] == "knowledge"
    updated = client.patch(
        f"/api/knowledge/{page['id']}",
        json={
            "expected_revision": 1,
            "title": "机密新草稿",
            "body": "新草稿秘密气泵",
            "references": [],
        },
    )
    assert updated.status_code == 200
    drain(app, owner)
    assert search(client, owner, "秘密").json()["total"] == 0
    assert search(client, owner, "阀门").json()["total"] == 1
    evidence = resolve(client, owner, hit)
    assert evidence.status_code == 200 and "检查周期" in evidence.json()["unit"]["text"]
    assert search(client, owner, "阀门", evidence_mode="original_evidence").json()["total"] == 0
    assert (
        search(client, owner, "阀门", kind="knowledge", evidence_mode="original_evidence").status_code == 422
    )
    assert resolve(client, owner, hit, evidence_mode="original_evidence").status_code == 422
    assert search(client, owner, "阀门", evidence_mode="reviewed_facts").status_code == 422
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert (
            session.scalar(
                select(ContentSearchDocument.id).where(ContentSearchDocument.asset_id == draft["id"])
            )
            is None
        )
    assert (
        client.post(f"/api/knowledge/{page['id']}/publish", json={"expected_revision": 2}).status_code == 200
    )
    assert search(client, owner, "阀门").json()["total"] == 0
    assert resolve(client, owner, hit).status_code == 409
    drain(app, owner)
    assert search(client, owner, "气泵").json()["items"][0]["source_revision"] == 2
    assert resolve(client, owner, hit).status_code == 409


def test_partial_and_long_document_exact_location_after_200_elements(content_platform):
    client, app, owner, _ = content_platform
    doc = parsed_doc(
        client, app, owner, "\n\n".join([f"普通段落 {i}" for i in range(220)] + ["尾部证据 特殊阀门 😀"])
    )
    with app.state.db.transaction(owner["tenant_id"]) as session:
        row = session.get(Document, doc["id"])
        changed = copy.deepcopy(row.ir)
        changed["elements"][-1]["metadata"]["exact_integer"] = 9223372036854775807
        changed.update(complete=False, quality="needs_review", warnings=["扫描页未识别"])
        changed["pages"] = [{"page_number": 1, "status": "needs_ocr", "reason": "OCR unavailable"}]
        row.ir, row.ir_fingerprint, row.state = changed, ir_fingerprint(changed), "partial"
        enqueue_content(session, row, "document")
    drain(app, owner)
    hit = search(client, owner, "特殊阀门").json()["items"][0]
    assert hit["status"] == "partial" and not hit["complete"] and hit["warnings"] == ["扫描页未识别"]
    assert hit["anchor"]["element_index"] > 200
    result = resolve(client, owner, hit).json()
    assert "尾部证据" in result["unit"]["text"] and result["coverage"]["pages"][0]["status"] == "needs_ocr"
    assert result["unit"]["evidence"]["page_number"] is None
    assert result["unit"]["metadata"]["exact_integer"] == "9223372036854775807"


def test_reparse_generation_rebuild_and_failed_old_content_disappears(content_platform):
    client, app, owner, _ = content_platform

    def current_search():
        response = search(client, owner, "阀门")
        assert response.status_code == 200, f"Content search HTTP {response.status_code}: {response.text}"
        return response.json()

    doc = parsed_doc(client, app, owner)
    drain(app, owner)
    hit = current_search()["items"][0]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        row = session.get(Document, doc["id"])
        row.state = "pending"
        enqueue_content(session, row, "document")
    assert current_search()["total"] == 0
    assert resolve(client, owner, hit).status_code == 409
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.add(
            Job(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                kind="document.parse",
                owner_id=owner["user_id"],
                payload={
                    "document_id": doc["id"],
                    "revision": 1,
                    "engine": "basic",
                    "expected_parse_revision": doc["parse_revision"],
                    "expected_ir_fingerprint": doc["ir_fingerprint"],
                },
            )
        )
    assert process_one(app.state.db, app.state.settings)
    drain(app, owner)
    new = current_search()["items"][0]
    assert new["parse_revision"] == 2 and new["source_revision"] == 1
    assert resolve(client, owner, hit).status_code == 409
    index = client.get("/api/search/content/status", params={"project_id": owner["project_id"]}).json()
    body = {
        "project_id": owner["project_id"],
        "kind": "document",
        "expected_generations": {"document": index["document"]["generation"]},
    }
    assert client.post("/api/search/content/rebuild", json=body).status_code == 202
    assert client.post("/api/search/content/rebuild", json=body).status_code == 409
    assert resolve(client, owner, new).status_code == 409
    assert current_search()["total"] == 0
    drain(app, owner)
    assert current_search()["total"] == 1


def test_project_tenant_membership_and_read_only_rebuild(content_platform):
    client, app, owner, other = content_platform
    parsed_doc(client, app, owner)
    drain(app, owner)
    hit = search(client, owner, "阀门").json()["items"][0]
    assert search(client, other, "阀门").status_code == 404
    assert resolve(client, other, hit).status_code == 404
    second = client.post("/api/projects", json={"name": "Another project"}).json()
    assert search(client, {"project_id": second["id"]}, "阀门").json()["total"] == 0
    assert resolve(client, {"project_id": second["id"]}, hit).status_code == 409
    person = client.post(
        "/api/users",
        json={
            "email": "content-audit@test.local",
            "display_name": "审计员",
            "password": "Content-Audit-Test-2026",
        },
    ).json()
    assert (
        client.post(
            "/api/members",
            json={"project_id": owner["project_id"], "user_id": person["id"], "role": "auditor"},
        ).status_code
        == 200
    )
    with app.state.db.transaction(owner["tenant_id"]) as session:
        slug = session.get(Tenant, owner["tenant_id"]).slug
    with TestClient(app) as reader:
        assert (
            reader.post(
                "/api/auth/login",
                json={"tenant_slug": slug, "email": person["email"], "password": "Content-Audit-Test-2026"},
            ).status_code
            == 200
        )
        assert search(reader, owner, "阀门").json()["total"] == 1
        assert resolve(reader, owner, hit).status_code == 200
        assert (
            reader.post(
                "/api/search/content/rebuild",
                json={"project_id": owner["project_id"], "kind": "all", "expected_generations": {}},
            ).status_code
            == 403
        )
        with app.state.db.transaction(owner["tenant_id"]) as session:
            session.execute(delete(Membership).where(Membership.user_id == person["id"]))
        assert search(reader, owner, "阀门").status_code == 404
        assert resolve(reader, owner, hit).status_code == 404


def test_index_failure_is_atomic_and_rebuild_recovers(content_platform, monkeypatch):
    client, app, owner, _ = content_platform
    doc = parsed_doc(client, app, owner)
    original = content_search_index.tokens
    calls = 0

    def fail_after_one(value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("private source text must never be persisted in failure")
        return original(value)

    monkeypatch.setattr(content_search_index, "tokens", fail_after_one)
    # Other kind may initialize first. Stop only once this project is failed.
    for _ in range(5):
        process_one_content_index(app.state.db, owner["tenant_id"])
    result = search(client, owner, "阀门").json()
    assert result["total"] == 0 and result["index"]["document"]["status"] == "failed"
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(func.count()).select_from(ContentSearchChunk)) == 0
        assert session.scalar(select(func.count()).select_from(ContentSearchDocument)) == 0
        assert session.get(Document, doc["id"]).state == "ready"
    monkeypatch.setattr(content_search_index, "tokens", original)
    generations = {k: v["generation"] for k, v in result["index"].items()}
    assert (
        client.post(
            "/api/search/content/rebuild",
            json={"project_id": owner["project_id"], "kind": "all", "expected_generations": generations},
        ).status_code
        == 202
    )
    drain(app, owner)
    assert search(client, owner, "阀门").json()["total"] == 1


def test_run_worker_entry_point_maintains_content(platform):
    client, app, owner, _ = platform
    upload(client, owner["project_id"], "正式入口全文阀门".encode())
    for _ in range(8):
        run_worker(app.state.settings, once=True)
    assert search(client, owner, "阀门").json()["total"] == 1
