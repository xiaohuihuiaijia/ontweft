import pytest
from fastapi.testclient import TestClient

from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
@pytest.mark.parametrize("revocation", ["membership", "disabled"])
def test_revoked_model_approval_cannot_publish(request, fixture_name, revocation, definition):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    created = client.post(
        "/api/users",
        json={
            "email": "model-reviewer@example.test",
            "display_name": "模型审核员",
            "password": "Model-Reviewer-Password",
        },
    )
    assert created.status_code == 201, created.text
    reviewer_id = created.json()["id"]
    assert (
        client.post(
            "/api/members",
            json={"project_id": owner["project_id"], "user_id": reviewer_id, "role": "reviewer"},
        ).status_code
        == 200
    )
    draft = client.post(
        "/api/changesets",
        json={
            "project_id": owner["project_id"],
            "title": "审核权限验证",
            "definition": definition,
            "expected_baseline_release_id": None,
        },
    )
    assert draft.status_code == 201, draft.text
    change = draft.json()
    validated = client.post(
        f"/api/changesets/{change['id']}/validate", json={"expected_revision": change["revision"]}
    )
    assert validated.status_code == 200, validated.text
    change = validated.json()
    with TestClient(app) as reviewer:
        slug = client.get("/api/auth/me").json()["tenant"]["slug"]
        assert (
            reviewer.post(
                "/api/auth/login",
                json={
                    "tenant_slug": slug,
                    "email": "model-reviewer@example.test",
                    "password": "Model-Reviewer-Password",
                },
            ).status_code
            == 200
        )
        approved = reviewer.post(
            f"/api/changesets/{change['id']}/approve", json={"expected_revision": change["revision"]}
        )
        assert approved.status_code == 200, approved.text
        change = approved.json()
    if revocation == "membership":
        assert (
            client.post(
                "/api/members",
                json={"project_id": owner["project_id"], "user_id": reviewer_id, "role": "member"},
            ).status_code
            == 200
        )
    else:
        assert client.post(f"/api/users/{reviewer_id}/disable").status_code == 200
    published = client.post(
        f"/api/changesets/{change['id']}/publish", json={"expected_revision": change["revision"]}
    )
    assert published.status_code == 409, published.text
    assert client.get("/api/releases", params={"project_id": owner["project_id"]}).json()["total"] == 0
    assert (
        client.get("/api/ontology/definition", params={"project_id": owner["project_id"]}).json()[
            "release_id"
        ]
        is None
    )
