"""Local snapshot bytes through real parser/DB/index; not a live Feishu import."""

import pytest
from ontology.content_access.models import DocumentSource
from ontology.documents.feishu_native import snapshot_bytes
from ontology.jobs.models import Job
from ontology.worker import process_one
from sqlalchemy import func, select

from tests.support.content import upload
from tests.support.content_search_pipeline import assert_bound_download, drain, search
from tests.support.document_reparse_pipeline import current, historical, history, reparse
from tests.support.feishu_native import native_fixture
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_native_snapshot_real_worker_history_search_and_download(request, fixture_name):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    original = snapshot_bytes(native_fixture())
    created = upload(client, owner["project_id"], original, "设备.feishu-docx.json")
    assert created["job"]["payload"]["engine"] == "basic"
    assert created["job"]["payload"]["configuration"]["adapter"] == "feishu-native/1.0"
    assert process_one(app.state.db, app.state.settings)
    doc = current(client, created["document"]["id"])
    assert doc["state"] == "ready", doc.get("error")
    assert doc["ir"]["format"] == "feishu_docx" and doc["parse_revision"] == 1
    drain(app, owner)
    found = search(client, owner, "季度检查")
    assert found["total"] == 1, found
    hit = found["items"][0]
    result = assert_bound_download(client, owner, hit, doc, original)
    assert result["unit"]["evidence"]["element_path"].endswith("/revisions/7/blocks/c01")
    first = history(client, doc["id"])["items"][0]
    assert first["engine"] == "basic" and first["origin"] == "worker"
    assert first["parser"] == "basic/feishu_docx"
    with app.state.db.transaction(owner["tenant_id"]) as session:
        jobs_before = session.scalar(select(func.count()).select_from(Job))
        # Local snapshot data must not manufacture a trusted Feishu source binding.
        assert session.scalar(select(func.count()).select_from(DocumentSource)) == 0
    assert reparse(client, doc, "docling").status_code == 422
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(func.count()).select_from(Job)) == jobs_before
    assert reparse(client, doc, "auto").status_code == 202
    assert process_one(app.state.db, app.state.settings)
    updated = current(client, doc["id"])
    assert updated["parse_revision"] == 2 and updated["ir_fingerprint"] == doc["ir_fingerprint"]
    old = historical(client, doc["id"], first["id"])
    assert old["ir"] == doc["ir"] and old["is_current"] is False
    assert history(client, doc["id"])["total"] == 2


def test_resources_remain_partial_and_cannot_be_published_as_complete_reference(platform):
    client, app, owner, _ = platform
    created = upload(
        client, owner["project_id"], snapshot_bytes(native_fixture(resources=True)), "资源.feishu-docx.json"
    )
    assert process_one(app.state.db, app.state.settings)
    doc = current(client, created["document"]["id"])
    assert doc["state"] == "partial" and not doc["ir"]["complete"]
    drain(app, owner)
    assert search(client, owner, "季度检查")["total"] == 1
    response = client.post(
        "/api/knowledge",
        json={
            "project_id": owner["project_id"],
            "title": "未完整引用",
            "body": "引用尚未取得的附件",
            "references": [
                {
                    "document_id": doc["id"],
                    "revision": doc["revision"],
                    "parse_revision": doc["parse_revision"],
                    "ir_fingerprint": doc["ir_fingerprint"],
                    "parse_id": doc["current_parse_id"],
                    "content_hash": doc["content_hash"],
                }
            ],
        },
    )
    assert response.status_code in {409, 422}, response.text
