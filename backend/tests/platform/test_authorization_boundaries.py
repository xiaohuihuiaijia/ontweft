"""Previously known resource IDs must not bypass current access boundaries."""

from fastapi.testclient import TestClient
from ontology.objects.models import Entity

from tests.support.platform import publish


def test_member_cannot_read_deleted_object_history_using_known_id(platform, definition):
    owner, app, tenant, _ = platform
    publish(owner, tenant["project_id"], definition)
    obj = owner.post(
        "/api/objects",
        json={
            "project_id": tenant["project_id"],
            "type_key": "Project",
            "properties": {"name": "撤回普通访问"},
        },
    ).json()
    member = owner.post(
        "/api/users",
        json={
            "email": "history-reader@example.test",
            "display_name": "普通成员",
            "password": "History-Reader-Password",
        },
    ).json()
    assert member["admin"] is False
    assert (
        owner.post(
            "/api/members",
            json={"project_id": tenant["project_id"], "user_id": member["id"], "role": "member"},
        ).status_code
        == 200
    )
    with TestClient(app) as reader:
        assert (
            reader.post(
                "/api/auth/login",
                json={
                    "tenant_slug": "alpha",
                    "email": "history-reader@example.test",
                    "password": "History-Reader-Password",
                },
            ).status_code
            == 200
        )
        assert reader.get(f"/api/objects/{obj['id']}/history").status_code == 200
        with app.state.db.transaction(tenant["tenant_id"]) as session:
            session.get(Entity, obj["id"]).deleted = True
        assert reader.get(f"/api/objects/{obj['id']}").status_code == 404
        response = reader.get(f"/api/objects/{obj['id']}/history")
        assert response.status_code == 404, response.text
        assert "撤回普通访问" not in response.text
