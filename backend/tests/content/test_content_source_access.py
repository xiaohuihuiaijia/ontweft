"""Stored source content remains scoped across every existing read surface."""

import time

import pytest
from ontology.content_access.models import DocumentSource, KnowledgeDependency, SourcePermission
from ontology.integrations.feishu.gateway import FeishuGatewayError, FeishuUserGateway, ResourcePermission
from sqlalchemy import select, update

from tests.support.content_search import drain, parsed_doc, published_page, resolve, search
from tests.support.feishu_employee import BASE, application, finish, start
from tests.support.feishu_employee import upstream as upstream  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.fixture(params=["platform", "pg_platform"])
def source_platform(request):
    return request.getfixturevalue(request.param)


@pytest.fixture
def protected(source_platform, upstream, monkeypatch):
    client, app, owner, _other = source_platform
    app_id = application(source_platform)
    assert finish(client, start(client, owner, app_id)["state"][0]).status_code == 200
    doc = parsed_doc(client, app, owner, "# 私密源标题\n\n受控星河预算记录。")
    allowed = {"view": True, "export": True, "calls": []}

    async def permission(self, kind, token, *, action):
        allowed["calls"].append(action)
        if allowed.get("unknown"):
            raise FeishuGatewayError("rate_limited", retryable=True)
        return ResourcePermission(self.context.reference(), kind, token, action, allowed[action])

    monkeypatch.setattr(FeishuUserGateway, "check_permission", permission)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        origin = DocumentSource(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            document_id=doc["id"],
            application_id=app_id,
            service="feishu",
            external_tenant_key="tenant-synthetic",
            resource_type="docx",
            resource_token="synthetic-resource",
        )
        session.add(origin)
    reference = {
        "document_id": doc["id"],
        "revision": doc["revision"],
        "parse_revision": doc["parse_revision"],
        "ir_fingerprint": doc["ir_fingerprint"],
        "content_hash": doc["content_hash"],
        "parse_id": doc["current_parse_id"],
    }
    page = published_page(client, owner, "受控星河预算派生结论", [reference])
    drain(app, owner)
    return client, app, owner, doc, page, app_id, allowed


def expire(app, owner):
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.execute(update(SourcePermission).values(valid_until=0))


def test_revocation_filters_counts_search_graph_history_download_and_jobs(protected):
    client, app, owner, doc, page, _, allowed = protected
    response = search(client, owner, "受控星河预算")
    assert response.status_code == 200, response.text
    assert response.json()["total"] >= 2
    hit = response.json()["items"][0]
    assert client.get(f"/api/documents/{doc['id']}/download").status_code == 200
    allowed["view"] = False
    expire(app, owner)
    for url in (
        f"/api/documents/{doc['id']}",
        f"/api/documents/{doc['id']}/download",
        f"/api/documents/{doc['id']}/parses",
        f"/api/documents/{doc['id']}/parses/{doc['current_parse_id']}",
        f"/api/knowledge/{page['id']}",
    ):
        response = client.get(url)
        assert response.status_code == 404, (url, response.text)
        assert "星河预算" not in response.text
    for route in ("documents", "knowledge", "jobs"):
        response = client.get("/api/" + route, params={"project_id": owner["project_id"]})
        assert response.status_code == 200, response.text
        assert response.json()["items"] == [] and response.json()["total"] == 0
    assert search(client, owner, "受控星河预算").json()["total"] == 0
    assert resolve(client, owner, hit).status_code == 409
    graph = client.get("/api/graph", params={"project_id": owner["project_id"], "kind": "knowledge"})
    assert graph.status_code == 200 and graph.json()["nodes"] == []
    history = client.get(f"/api/knowledge/{page['id']}/history")
    assert history.status_code == 200 and history.json()["total"] == 0
    assert (
        client.post(f"/api/knowledge/{page['id']}/publish", json={"expected_revision": 1}).status_code == 404
    )
    audit = client.get("/api/audit", params={"project_id": owner["project_id"]})
    assert audit.status_code == 200
    assert all(item["resource_id"] not in {doc["id"], page["id"]} for item in audit.json()["items"])


def test_removing_citation_does_not_declassify_retained_derived_text(protected):
    client, app, owner, _, page, _, allowed = protected
    edited = client.patch(
        f"/api/knowledge/{page['id']}",
        json={"expected_revision": 1, "title": "派生", "body": page["body"], "references": []},
    )
    assert edited.status_code == 200, edited.text
    assert (
        client.post(f"/api/knowledge/{page['id']}/publish", json={"expected_revision": 2}).status_code == 200
    )
    with app.state.db.transaction(owner["tenant_id"]) as session:
        deps = session.scalars(
            select(KnowledgeDependency).where(KnowledgeDependency.page_id == page["id"])
        ).all()
        assert {dep.revision for dep in deps} == {1, 2}
    allowed["view"] = False
    expire(app, owner)
    assert client.get(f"/api/knowledge/{page['id']}").status_code == 404


def test_unknown_permission_never_reuses_expired_positive_or_reports_empty_success(protected):
    client, app, owner, _, _, _, allowed = protected
    allowed["unknown"] = True
    expire(app, owner)
    response = client.get("/api/documents", params={"project_id": owner["project_id"]})
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "source_authorization_unknown"
    assert "私密" not in response.text and "total" not in response.json()
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert all(
            row.status == "unknown" and row.valid_until <= time.time()
            for row in session.scalars(select(SourcePermission))
        )


def test_export_is_checked_separately_and_disconnect_invalidates_fresh_cache(protected):
    client, _app, owner, doc, _, app_id, allowed = protected
    allowed["export"] = False
    assert client.get(f"/api/documents/{doc['id']}").status_code == 200
    assert client.get(f"/api/documents/{doc['id']}/download").status_code == 404
    calls = len(allowed["calls"])
    assert (
        client.post(
            BASE + f"/applications/{app_id}/disconnect", json={"project_id": owner["project_id"]}
        ).status_code
        == 200
    )
    assert client.get(f"/api/documents/{doc['id']}").status_code == 404
    assert len(allowed["calls"]) == calls


def test_readonly_project_member_can_connect_own_account_but_cannot_preview_import(protected):
    from ontology.identity.models import Membership

    client, app, owner, doc, _, app_id, _ = protected
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.scalar(
            select(Membership).where(
                Membership.user_id == owner["user_id"], Membership.project_id == owner["project_id"]
            )
        ).role = "auditor"
    listing = client.get(BASE + "/applications", params={"project_id": owner["project_id"]})
    assert listing.status_code == 200
    assert finish(client, start(client, owner, app_id)["state"][0]).status_code == 200
    assert client.get(f"/api/documents/{doc['id']}").status_code == 200
    response = client.post(
        BASE + f"/applications/{app_id}/selection-preview",
        json={
            "project_id": owner["project_id"],
            "expected_account_revision": 1,
            "selections": [{"space_id": "s", "node_token": "n"}],
        },
    )
    assert response.status_code == 403


def test_parse_worker_rechecks_source_before_work_and_keeps_previous_parse(protected):
    from ontology.documents.storage_models import Document
    from ontology.jobs.models import Job
    from ontology.worker import process_one

    client, app, owner, doc, _, _, allowed = protected
    requested = client.post(
        f"/api/documents/{doc['id']}/reparse",
        json={
            "expected_revision": doc["revision"],
            "expected_parse_revision": doc["parse_revision"],
            "expected_ir_fingerprint": doc["ir_fingerprint"],
            "engine": "basic",
        },
    )
    assert requested.status_code == 202, requested.text
    allowed["view"] = False
    expire(app, owner)
    assert client.get(f"/api/documents/{doc['id']}").status_code == 404
    assert process_one(app.state.db, app.state.settings)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        job = session.get(Job, requested.json()["job"]["id"])
        assert job.status == "failed" and "来源授权" in job.error
        retained = session.get(Document, doc["id"])
        assert retained.parse_revision == doc["parse_revision"] and retained.ir == doc["ir"]


def test_provenance_and_inherited_dependencies_cannot_be_removed(protected):
    from sqlalchemy import delete
    from sqlalchemy.exc import DBAPIError

    _, app, owner, _, _, _, _ = protected
    for model in (DocumentSource, KnowledgeDependency):
        with pytest.raises(DBAPIError), app.state.db.transaction(owner["tenant_id"]) as session:
            session.execute(delete(model))


def test_later_explicit_denial_fences_an_inflight_positive_observation(protected, monkeypatch):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from ontology.integrations.feishu.gateway import WikiNode

    from tests.support.feishu_employee import account

    client, app, owner, doc, _, app_id, _ = protected
    entered, release = threading.Event(), threading.Event()
    invocations = []

    async def permission(self, kind, token, *, action):
        invocations.append(action)
        if len(invocations) == 1:
            entered.set()
            deadline = time.monotonic() + 15
            while not release.is_set():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            allowed = True
        else:
            allowed = False
        return ResourcePermission(self.context.reference(), kind, token, action, allowed)

    async def node(self, token):
        return WikiNode(
            self.context.reference(),
            "space",
            token,
            "docx",
            "synthetic-resource",
            "origin",
            None,
            None,
            False,
            "private",
        )

    monkeypatch.setattr(FeishuUserGateway, "check_permission", permission)
    monkeypatch.setattr(FeishuUserGateway, "get_wiki_node", node)
    expire(app, owner)
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(client.get, f"/api/documents/{doc['id']}")
        try:
            assert entered.wait(10)
            preview = pool.submit(
                client.post,
                BASE + f"/applications/{app_id}/selection-preview",
                json={
                    "project_id": owner["project_id"],
                    "expected_account_revision": account(app, owner).revision,
                    "selections": [{"space_id": "space", "node_token": "node"}],
                },
            ).result(10)
            assert preview.status_code == 200 and preview.json()["items"][0]["status"] == "denied"
        finally:
            release.set()
        response = pending.result(10)
    assert response.status_code == 404, response.text
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(SourcePermission)).status == "denied"


def test_source_rows_are_tenant_scoped_and_permission_cannot_name_foreign_user(protected, source_platform):
    from sqlalchemy.exc import DBAPIError

    _, app, owner, _, _, _, _ = protected
    other = source_platform[3]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        source_id = session.scalar(select(DocumentSource.id))
    with pytest.raises(DBAPIError) as rejected, app.state.db.transaction(owner["tenant_id"]) as session:
        session.add(
            SourcePermission(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                source_id=source_id,
                user_id=other["user_id"],
                action="view",
                account_revision=1,
                application_revision=1,
                user_revision=1,
            )
        )
        session.flush()
    if app.state.db.engine.dialect.name == "postgresql":
        detail = rejected.value.orig.args[0]
        assert detail["C"] == "23503"
        assert detail["n"] == "content_source_permissions_tenant_id_user_id_fkey"
    else:
        assert rejected.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_FOREIGNKEY"
    if app.state.db.engine.dialect.name == "postgresql":
        for scope in (None, other["tenant_id"]):
            with app.state.db.transaction(scope) as session:
                for model in (DocumentSource, KnowledgeDependency, SourcePermission):
                    assert session.scalars(select(model)).all() == []
        app.state.db.verify_runtime_role()


def test_a_colleague_cannot_borrow_fresh_owner_permission_observations(protected):
    from fastapi.testclient import TestClient
    from ontology.identity.models import Membership, Tenant, User
    from ontology.identity.security import PASSWORDS

    _, app, owner, doc, _, _, _ = protected
    with app.state.db.transaction(owner["tenant_id"]) as session:
        tenant = session.get(Tenant, owner["tenant_id"])
        slug = tenant.slug
        colleague = User(
            tenant_id=owner["tenant_id"],
            email="scope-reader@example.test",
            display_name="读者",
            password_hash=PASSWORDS.hash("Scoped-Reader-Only-2026"),
        )
        session.add(colleague)
        session.flush()
        session.add(
            Membership(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                user_id=colleague.id,
                role="auditor",
            )
        )
    with TestClient(app) as second:
        assert (
            second.post(
                "/api/auth/login",
                json={
                    "tenant_slug": slug,
                    "email": "scope-reader@example.test",
                    "password": "Scoped-Reader-Only-2026",
                },
            ).status_code
            == 200
        )
        assert second.get(f"/api/documents/{doc['id']}").status_code == 404
        response = second.get("/api/documents", params={"project_id": owner["project_id"]})
        assert response.status_code == 200 and response.json()["total"] == 0
        applications = second.get(BASE + "/applications", params={"project_id": owner["project_id"]})
        assert applications.status_code == 200 and applications.json()["items"][0]["account"] is None


def test_invalid_project_shapes_reach_validation_without_database_or_remote_work(platform):
    client, _, _, _ = platform
    for invalid in ({"id": "project"}, ["project"], True):
        response = client.post("/api/search/content", json={"project_id": invalid, "q": "示例"})
        assert response.status_code == 422, response.text
