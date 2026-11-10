"""Content retrieval through real transactions, published versions and evidence."""

import pytest
from ontology.search.content.index import process_one_content_index
from ontology.worker import process_one

from tests.support.content import upload
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.fixture(params=["platform", "pg_platform"])
def content_platform(request):
    return request.getfixturevalue(request.param)


def drain(app, owner):
    for _ in range(50):
        if not process_one_content_index(app.state.db, owner["tenant_id"]):
            break
    else:
        pytest.fail("Content index did not become idle")


def search(client, owner, q, **options):
    return client.post("/api/search/content", json={"project_id": owner["project_id"], "q": q, **options})


def resolve(client, owner, hit, **options):
    return client.post(
        "/api/search/content/evidence",
        json={
            "project_id": owner["project_id"],
            "chunk_id": hit["id"],
            "expected_generation": hit["generation"],
            "expected_fingerprint": hit["source_fingerprint"],
            "expected_revision": hit["source_revision"],
            "expected_parse_revision": hit["parse_revision"],
            **options,
        },
    )


def parsed_doc(client, app, owner, text="# 设备手册\n\n南京市长江大桥阀门 ＡＩ检索。"):
    response = upload(client, owner["project_id"], text.encode(), "设备手册.md")
    assert process_one(app.state.db, app.state.settings)
    doc = client.get(f"/api/documents/{response['document']['id']}").json()
    assert doc["state"] == "ready", doc.get("error")
    return doc


def published_page(client, owner, body="发布知识 阀门检查周期", references=None):
    response = client.post(
        "/api/knowledge",
        json={
            "project_id": owner["project_id"],
            "title": "正式知识",
            "body": body,
            "references": references or [],
        },
    )
    assert response.status_code == 201, response.text
    page = response.json()
    response = client.post(f"/api/knowledge/{page['id']}/publish", json={"expected_revision": 1})
    assert response.status_code == 200, response.text
    return page
