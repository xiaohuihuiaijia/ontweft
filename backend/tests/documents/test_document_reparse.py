"""API and actual worker verification of immutable parse histories and retries."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from ontology import worker
from ontology.documents import parse_document
from ontology.documents.history_models import DocumentParseVersion
from ontology.documents.storage_models import Document
from ontology.identity.models import Membership, Tenant, User
from ontology.jobs.models import Job
from ontology.knowledge.models import KnowledgePage, KnowledgeRevision
from ontology.search.content.index import process_one_content_index
from ontology.search.content.units import ir_fingerprint
from sqlalchemy import func, select

from tests.support.content import upload
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.fixture(params=["platform", "pg_platform"])
def reparse_platform(request):
    return request.getfixturevalue(request.param)


def parsed(platform, text="旧解析原文 阀门证据", project_id=None):
    client, app, owner, _ = platform
    uploaded = upload(client, project_id or owner["project_id"], text.encode("utf-8"), "evidence.txt")
    assert worker.process_one(app.state.db, app.state.settings)
    response = client.get(f"/api/documents/{uploaded['document']['id']}")
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["state"] == "ready", document.get("error")
    assert document["parse_revision"] == 1 and document["current_parse_id"]
    return document


def baseline(document):
    return {
        "expected_revision": document["revision"],
        "expected_parse_revision": document["parse_revision"],
        "expected_ir_fingerprint": document["ir_fingerprint"],
        "engine": "basic",
    }


def reparse(client, document):
    return client.post(f"/api/documents/{document['id']}/reparse", json=baseline(document))


def reference(document, *, fixed=True):
    result = {
        "document_id": document["id"],
        "revision": document["revision"],
        "parse_revision": document["parse_revision"],
        "ir_fingerprint": document["ir_fingerprint"],
        "content_hash": document["content_hash"],
        "element_path": document["ir"]["elements"][0]["evidence"]["element_path"],
    }
    if fixed:
        result["parse_id"] = document["current_parse_id"]
    return result


def drain(db, tenant_id):
    for _ in range(30):
        if not process_one_content_index(db, tenant_id):
            return
    pytest.fail("Content fixture did not become idle")


def test_concurrent_reparse_creates_at_most_one_job_and_old_retry_is_exclusive(reparse_platform):
    client, app, owner, _ = reparse_platform
    document = parsed(reparse_platform)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: reparse(client, document), range(2)))
    assert sorted(response.status_code for response in responses) == [202, 409]
    first = next(response.json()["job"] for response in responses if response.status_code == 202)
    assert client.post(f"/api/jobs/{first['id']}/cancel").status_code == 200
    second = reparse(client, document)
    assert second.status_code == 202, second.text
    assert client.post(f"/api/jobs/{first['id']}/retry").status_code == 409
    with app.state.db.transaction(owner["tenant_id"]) as session:
        jobs = session.scalars(select(Job).where(Job.project_id == owner["project_id"])).all()
        assert len([job for job in jobs if job.status in {"pending", "running", "retry_wait"}]) == 1
        assert session.get(Document, document["id"]).parse_revision == 1
    assert worker.process_one(app.state.db, app.state.settings)
    assert reparse(client, document).status_code == 409
    assert client.post(f"/api/jobs/{first['id']}/retry").status_code == 409
    history = client.get(f"/api/documents/{document['id']}/parses").json()
    assert history["total"] == 2
    assert [item["parse_revision"] for item in history["items"]] == [2, 1]


def test_retry_and_new_reparse_share_one_admission_boundary(reparse_platform):
    client, app, owner, _ = reparse_platform
    document = parsed(reparse_platform)
    queued = reparse(client, document)
    assert queued.status_code == 202
    job_id = queued.json()["job"]["id"]
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    start = Barrier(2)

    def submit(retry):
        start.wait(timeout=10)
        return client.post(f"/api/jobs/{job_id}/retry") if retry else reparse(client, document)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, (True, False)))
    assert sorted(response.status_code for response in responses) == [202, 409] or sorted(
        response.status_code for response in responses
    ) == [200, 409]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        jobs = session.scalars(select(Job).where(Job.project_id == owner["project_id"])).all()
        assert len([job for job in jobs if job.status in {"pending", "running", "retry_wait"}]) == 1
        assert session.get(Document, document["id"]).parse_revision == document["parse_revision"]
    assert worker.process_one(app.state.db, app.state.settings)
    assert client.get(f"/api/documents/{document['id']}").json()["parse_revision"] == 2


def test_history_reads_current_authority_and_wrong_document_scope_is_hidden(reparse_platform):
    client, app, owner, other = reparse_platform
    document = parsed(reparse_platform)
    path = f"/api/documents/{document['id']}/parses"
    detail_path = f"{path}/{document['current_parse_id']}"
    history = client.get(path).json()
    assert history["total"] == 1 and "ir" not in history["items"][0]
    detail = client.get(detail_path)
    assert detail.status_code == 200, detail.text
    assert detail.json()["ir"]["elements"][0]["text"] == "旧解析原文 阀门证据"
    assert "旧解析原文" in detail.json()["ir_json"]
    assert client.get(detail.json()["download_url"]).status_code == 200
    second = client.post("/api/projects", json={"name": "Separate document project"}).json()
    second_document = parsed(reparse_platform, project_id=second["id"])
    assert (
        client.get(
            f"/api/documents/{second_document['id']}/parses/{document['current_parse_id']}"
        ).status_code
        == 404
    )
    person = client.post(
        "/api/users",
        json={
            "email": "parse-reader@example.test",
            "display_name": "审计读者",
            "password": "Parse-Reader-Test-2026",
        },
    ).json()
    assert (
        client.post(
            "/api/members",
            json={
                "project_id": owner["project_id"],
                "user_id": person["id"],
                "role": "auditor",
            },
        ).status_code
        == 200
    )
    with app.state.db.transaction(owner["tenant_id"]) as session:
        slug = session.get(Tenant, owner["tenant_id"]).slug
    with TestClient(app) as reader:
        assert (
            reader.post(
                "/api/auth/login",
                json={
                    "tenant_slug": slug,
                    "email": person["email"],
                    "password": "Parse-Reader-Test-2026",
                },
            ).status_code
            == 200
        )
        assert reader.get(path).status_code == 200 and reader.get(detail_path).status_code == 200
        assert reparse(reader, document).status_code == 403
        assert reader.get(f"/api/documents/{second_document['id']}/parses").status_code == 404
        with app.state.db.transaction(owner["tenant_id"]) as session:
            member = session.scalar(
                select(Membership).where(
                    Membership.project_id == owner["project_id"],
                    Membership.user_id == person["id"],
                )
            )
            session.delete(member)
        assert reader.get(path).status_code == 404 and reader.get(detail_path).status_code == 404
    # Same-tenant scope is checked above. A different authenticated tenant also
    # cannot discover the parse through a valid document/parse-id pair.
    with app.state.db.transaction(other["tenant_id"]) as session:
        tenant_slug = session.get(Tenant, other["tenant_id"]).slug
        email = session.get(User, other["user_id"]).email
    password = (
        "Postgres-Integration-Password"
        if app.state.db.engine.dialect.name == "postgresql"
        else "Safe-Test-Password-2026"
    )
    with TestClient(app) as outsider:
        assert (
            outsider.post(
                "/api/auth/login",
                json={
                    "tenant_slug": tenant_slug,
                    "email": email,
                    "password": password,
                },
            ).status_code
            == 200
        )
        assert outsider.get(path).status_code == 404
        assert outsider.get(detail_path).status_code == 404


def test_historical_parse_references_remain_bound_and_stale_preview_is_rejected(reparse_platform):
    client, app, owner, _ = reparse_platform
    old = parsed(reparse_platform)
    assert reparse(client, old).status_code == 202
    assert worker.process_one(app.state.db, app.state.settings)
    latest = client.get(f"/api/documents/{old['id']}").json()
    assert latest["parse_revision"] == 2
    stale_preview = client.post(
        "/api/knowledge",
        json={
            "project_id": owner["project_id"],
            "title": "Stale preview",
            "body": "must refresh",
            "references": [reference(old, fixed=False)],
        },
    )
    assert stale_preview.status_code == 409
    page = client.post(
        "/api/knowledge",
        json={
            "project_id": owner["project_id"],
            "title": "Historical evidence",
            "body": "绑定旧解析知识",
            "references": [reference(old)],
        },
    )
    assert page.status_code == 201, page.text
    assert page.json()["references"][0]["parse_id"] == old["current_parse_id"]
    assert (
        client.post(f"/api/knowledge/{page.json()['id']}/publish", json={"expected_revision": 1}).status_code
        == 200
    )
    for field, value in (("parse_revision", 99), ("ir_fingerprint", "0" * 64), ("content_hash", "0" * 64)):
        forged = {**reference(old), field: value}
        response = client.post(
            "/api/knowledge",
            json={
                "project_id": owner["project_id"],
                "title": "Forged",
                "body": "must reject",
                "references": [forged],
            },
        )
        assert response.status_code == 422
    detail = client.get(f"/api/documents/{old['id']}/parses/{old['current_parse_id']}").json()
    assert detail["parse_revision"] == 1 and not detail["is_current"]


def test_legacy_citations_are_not_rewritten_or_automatically_bound(reparse_platform):
    client, app, owner, _ = reparse_platform
    document = parsed(reparse_platform)
    legacy = {
        key: value
        for key, value in reference(document).items()
        if key in {"document_id", "revision", "element_path"}
    }
    with app.state.db.transaction(owner["tenant_id"]) as session:
        page = KnowledgePage(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            title="旧知识",
            body="未绑定旧知识原文",
            references=[legacy],
            revision=1,
            published_revision=1,
            author_id=owner["user_id"],
        )
        session.add(page)
        session.flush()
        revision = KnowledgeRevision(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            page_id=page.id,
            title=page.title,
            body=page.body,
            references=[legacy],
            revision=1,
            author_id=owner["user_id"],
        )
        session.add(revision)
        session.flush()
        page_id, revision_id = page.id, revision.id
    drain(app.state.db, owner["tenant_id"])
    response = client.post(
        "/api/search/content", json={"project_id": owner["project_id"], "q": "未绑定", "kind": "knowledge"}
    )
    assert response.status_code == 200, response.text
    hit = response.json()["items"][0]
    evidence = client.post(
        "/api/search/content/evidence",
        json={
            "project_id": owner["project_id"],
            "chunk_id": hit["id"],
            "expected_generation": hit["generation"],
            "expected_fingerprint": hit["source_fingerprint"],
            "expected_revision": hit["source_revision"],
            "expected_parse_revision": hit["parse_revision"],
        },
    )
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["references"] == [{**legacy, "status": "legacy_unbound"}]
    assert client.post(f"/api/knowledge/{page_id}/publish", json={"expected_revision": 1}).status_code == 422
    updated = client.patch(
        f"/api/knowledge/{page_id}",
        json={
            "expected_revision": 1,
            "title": "重新选择证据",
            "body": "新修订明确绑定",
            "references": [reference(document, fixed=False)],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2
    assert updated.json()["references"][0]["parse_id"] == document["current_parse_id"]
    assert client.post(f"/api/knowledge/{page_id}/publish", json={"expected_revision": 2}).status_code == 200
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.get(KnowledgeRevision, revision_id).references == [legacy]


def test_configuration_or_current_ir_changes_cannot_be_committed_as_another_parse(reparse_platform):
    client, app, owner, _ = reparse_platform
    document = parsed(reparse_platform)
    result = reparse(client, document)
    assert result.status_code == 202
    job_id = result.json()["job"]["id"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        job = session.get(Job, job_id)
        job.payload = {**job.payload, "configuration_fingerprint": "0" * 64}
    assert worker.process_one(app.state.db, app.state.settings)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.get(Job, job_id).status == "failed"
        current = session.get(Document, document["id"])
        assert current.parse_revision == 1 and current.ir_fingerprint == document["ir_fingerprint"]
        assert session.scalar(select(func.count()).select_from(DocumentParseVersion)) == 1
        altered = deepcopy(current.ir)
        altered["elements"][0]["text"] = "untrusted changed IR"
        current.ir = altered
    assert reparse(client, document).status_code == 409
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(func.count()).select_from(DocumentParseVersion)) == 1


def test_failed_history_insert_rolls_back_parse_replacement_and_preserves_last_good_version(
    reparse_platform, monkeypatch
):
    client, app, owner, _ = reparse_platform
    document = parsed(reparse_platform)
    drain(app.state.db, owner["tenant_id"])
    queued = reparse(client, document).json()["job"]
    original = worker.capture_current_parse

    def fail_after_append(session, doc, *, job=None):
        result = original(session, doc, job=job)
        if job is not None:
            session.flush()
            raise RuntimeError("Synthetic commit boundary failure after history INSERT")
        return result

    monkeypatch.setattr(worker, "capture_current_parse", fail_after_append)
    assert worker.process_one(app.state.db, app.state.settings)
    current = client.get(f"/api/documents/{document['id']}").json()
    assert current["state"] == "ready" and current["parse_revision"] == 1
    assert current["ir"] == document["ir"] and current["current_parse_id"] == document["current_parse_id"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(func.count()).select_from(DocumentParseVersion)) == 1
        assert session.get(Job, queued["id"]).status == "failed"
    found = client.post("/api/search/content", json={"project_id": owner["project_id"], "q": "阀门"})
    assert found.status_code == 200 and found.json()["total"] == 1


def test_queued_reparse_rechecks_current_permission_before_new_history(reparse_platform):
    client, app, owner, _ = reparse_platform
    document = parsed(reparse_platform)
    queued = reparse(client, document).json()["job"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        member = session.scalar(
            select(Membership).where(
                Membership.project_id == owner["project_id"],
                Membership.user_id == owner["user_id"],
            )
        )
        member.role = "auditor"
    assert worker.process_one(app.state.db, app.state.settings)
    assert reparse(client, document).status_code == 403
    assert (
        client.get(f"/api/documents/{document['id']}/parses/{document['current_parse_id']}").status_code
        == 200
    )
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.get(Job, queued["id"]).status == "failed"
        assert session.get(Document, document["id"]).parse_revision == 1
        assert session.scalar(select(func.count()).select_from(DocumentParseVersion)) == 1


def test_preupgrade_retained_ir_is_captured_as_legacy_without_inventing_configuration(reparse_platform):
    client, app, owner, _ = reparse_platform
    content = "迁移前保留的原文😀".encode()
    uploaded = upload(client, owner["project_id"], content, "legacy.txt")
    document_id = uploaded["document"]["id"]
    assert client.post(f"/api/jobs/{uploaded['job']['id']}/cancel").status_code == 200
    ir = parse_document(content, "legacy.txt", source_id=document_id, source_revision="1").model_dump(
        mode="json"
    )
    # This fixture represents an actual pre-upgrade shape: valid retained IR,
    # parse_revision=0 and no historical snapshot or recorded configuration.
    with app.state.db.transaction(owner["tenant_id"]) as session:
        doc = session.get(Document, document_id)
        doc.ir, doc.state, doc.ir_fingerprint = ir, "ready", None
        assert doc.parse_revision == 0
    preview = client.get(f"/api/documents/{document_id}").json()
    assert preview["current_parse_id"] is None and preview["ir_fingerprint"] == ir_fingerprint(ir)
    assert client.get(f"/api/documents/{document_id}/parses").json()["total"] == 0
    response = reparse(client, preview)
    assert response.status_code == 202, response.text
    history = client.get(f"/api/documents/{document_id}/parses").json()
    assert history["total"] == 1
    legacy = history["items"][0]
    assert (legacy["parse_revision"], legacy["origin"], legacy["engine"], legacy["job_id"]) == (
        0,
        "legacy",
        "legacy",
        None,
    )
    assert legacy["configuration"] == {} and legacy["configuration_fingerprint"] == ir_fingerprint({})
    assert worker.process_one(app.state.db, app.state.settings)
    history = client.get(f"/api/documents/{document_id}/parses").json()
    assert [(row["parse_revision"], row["origin"]) for row in history["items"]] == [
        (1, "worker"),
        (0, "legacy"),
    ]
    detail = client.get(f"/api/documents/{document_id}/parses/{legacy['id']}").json()
    assert detail["ir"] == ir


def test_permission_revoked_during_real_parser_execution_blocks_finish(reparse_platform, monkeypatch):
    client, app, owner, _ = reparse_platform
    document = parsed(reparse_platform)
    queued = reparse(client, document).json()["job"]
    original = worker.subprocess.run
    calls = []

    def revoke_after_parser(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result.returncode == 0
        with app.state.db.transaction(owner["tenant_id"]) as session:
            member = session.scalar(
                select(Membership).where(
                    Membership.project_id == owner["project_id"],
                    Membership.user_id == owner["user_id"],
                )
            )
            member.role = "auditor"
        calls.append(queued["id"])
        return result

    monkeypatch.setattr(worker.subprocess, "run", revoke_after_parser)
    assert worker.process_one(app.state.db, app.state.settings)
    assert calls == [queued["id"]]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.get(Job, queued["id"]).status == "failed"
        doc = session.get(Document, document["id"])
        assert doc.state == "ready" and doc.parse_revision == 1
        assert doc.ir == document["ir"]
        assert session.scalar(select(func.count()).select_from(DocumentParseVersion)) == 1


@pytest.mark.parametrize(
    "engine,file_format,package,pdf_mode",
    [
        ("docling", "docx", "docling-core", "native"),
        ("docling", "docx", "python-docx", "native"),
        ("docling", "docx", "lxml", "native"),
        ("docling", "xlsx", "openpyxl", "native"),
        ("docling", "xlsx", "defusedxml", "native"),
        ("docling", "pdf", "pypdfium2", "native"),
        ("docling", "pdf", "pypdf", "native"),
        ("docling", "pdf", "docling-ibm-models", "layout"),
        ("docling", "pdf", "torch", "layout"),
        ("basic", "docx", "defusedxml", "native"),
        ("basic", "xlsx", "openpyxl", "native"),
        ("basic", "pdf", "pypdfium2", "native"),
    ],
)
def test_direct_dependency_change_rejects_frozen_parse_job(
    monkeypatch, engine, file_format, package, pdf_mode
):
    from types import SimpleNamespace

    import ontology.documents.history as document_history
    from ontology.core.errors import DomainError

    monkeypatch.setenv("ONTOLOGY_DOCLING_PDF_MODE", pdf_mode)
    document = SimpleNamespace(id="synthetic-document", revision=1, parse_revision=0, ir_fingerprint=None)
    job = SimpleNamespace(payload=document_history.parse_payload(document, engine, file_format))
    configuration = deepcopy(job.payload["configuration"])
    assert package in configuration["packages"]
    document_history.validate_job_configuration(job)
    original = document_history.version

    def changed_dependency(name):
        return "synthetic-upgrade" if name == package else original(name)

    monkeypatch.setattr(document_history, "version", changed_dependency)
    with pytest.raises(DomainError) as rejected:
        document_history.validate_job_configuration(job)
    assert rejected.value.status == 409
    assert job.payload["configuration"] == configuration  # Never silently restamp an old job.


@pytest.mark.parametrize(
    "engine,file_format,unrelated",
    [
        ("basic", "markdown", "openpyxl"),
        ("docling", "pdf", "torch"),
    ],
)
def test_unrelated_dependency_change_does_not_invalidate_selected_parse_path(
    monkeypatch, engine, file_format, unrelated
):
    from types import SimpleNamespace

    import ontology.documents.history as document_history

    monkeypatch.setenv("ONTOLOGY_DOCLING_PDF_MODE", "native")
    document = SimpleNamespace(id="synthetic-document", revision=1, parse_revision=0, ir_fingerprint=None)
    job = SimpleNamespace(payload=document_history.parse_payload(document, engine, file_format))
    assert unrelated not in job.payload["configuration"]["packages"]
    original = document_history.version
    monkeypatch.setattr(
        document_history, "version", lambda name: "synthetic-upgrade" if name == unrelated else original(name)
    )
    document_history.validate_job_configuration(job)


def test_direct_dependency_upgrade_blocks_queued_docling_job_before_parser(reparse_platform, monkeypatch):
    import io

    import ontology.documents.history as document_history

    docx = pytest.importorskip("docx")
    pytest.importorskip("docling")
    client, app, owner, _ = reparse_platform
    source = docx.Document()
    source.add_paragraph("真实Word已确认的旧解析")
    buffer = io.BytesIO()
    source.save(buffer)
    uploaded = upload(client, owner["project_id"], buffer.getvalue(), "dependencies.docx")
    assert worker.process_one(app.state.db, app.state.settings)
    old = client.get(f"/api/documents/{uploaded['document']['id']}").json()
    assert old["state"] == "ready" and old["current_parse_id"]
    queued = client.post(f"/api/documents/{old['id']}/reparse", json={**baseline(old), "engine": "docling"})
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job"]["id"]
    before = deepcopy(queued.json()["job"]["payload"])
    original = document_history.version
    calls = []

    def unexpected_parser(*args, **kwargs):
        calls.append(True)
        raise AssertionError("A changed dependency must be rejected before spawning the parser")

    monkeypatch.setattr(
        document_history,
        "version",
        lambda name: "synthetic-upgrade" if name == "python-docx" else original(name),
    )
    monkeypatch.setattr(worker.subprocess, "run", unexpected_parser)
    assert worker.process_one(app.state.db, app.state.settings)
    assert calls == []
    with app.state.db.transaction(owner["tenant_id"]) as session:
        job = session.get(Job, job_id)
        assert job.status == "failed" and "解析环境或配置已改变" in job.error
        assert job.payload == before
        doc = session.get(Document, old["id"])
        assert doc.parse_revision == 1 and doc.ir_fingerprint == old["ir_fingerprint"]
        assert doc.state == "ready" and doc.ir == old["ir"]
        assert session.scalar(select(func.count()).select_from(DocumentParseVersion)) == 1
