"""Proposal history remains reachable beyond the first page without broadening access."""

from uuid import uuid4

import pytest
from ontology.identity.models import Membership
from ontology.intelligence.models import ModelConnection
from ontology.intelligence.proposal_models import AIProposal
from sqlalchemy import select
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.fixture(params=["platform", "pg_platform"])
def paged_platform(request):
    return request.getfixturevalue(request.param)


def seed_history(app, ids, count):
    identities = [str(uuid4()) for _ in range(count)]
    with app.state.db.transaction(ids["tenant_id"], write=True) as session:
        connection = ModelConnection(
            tenant_id=ids["tenant_id"], name="Synthetic listing fixture", base_url="https://example.test/v1",
        )
        session.add(connection)
        session.flush()
        for identity in identities:
            session.add(AIProposal(
                id=identity, tenant_id=ids["tenant_id"], project_id=ids["project_id"],
                owner_id=ids["user_id"], connection_id=connection.id, connection_revision=1,
                created_at="2026-11-11T00:00:00+00:00", inputs={"documents": [], "sources": []},
            ))
    return sorted(identities, reverse=True)


def test_proposal_pagination_reaches_all_rows_with_stable_ties_and_scoped_total(paged_platform):
    client, app, owner, other = paged_platform
    expected = seed_history(app, owner, 109)
    second_project = client.post("/api/projects", json={"name": "Separate history"}).json()["id"]
    seed_history(app, {**owner, "project_id": second_project}, 4)
    seed_history(app, other, 7)
    params = {"project_id": owner["project_id"]}
    first = client.get("/api/ai/proposals", params=params)
    assert first.status_code == 200, first.text
    first = first.json()
    assert (first["total"], first["limit"], first["offset"]) == (109, 100, 0)
    assert [item["id"] for item in first["items"]] == expected[:100]
    assert all("candidates" not in item and "definition" not in item for item in first["items"])
    last = client.get("/api/ai/proposals", params={**params, "offset": 100}).json()
    assert last["total"] == 109 and [item["id"] for item in last["items"]] == expected[100:]
    collected = []
    for offset in range(0, 109, 17):
        result = client.get("/api/ai/proposals", params={**params, "limit": 17, "offset": offset}).json()
        assert (result["total"], result["limit"], result["offset"]) == (109, 17, offset)
        collected.extend(item["id"] for item in result["items"])
    assert collected == expected
    assert client.get("/api/ai/proposals", params={**params, "offset": 109}).json()["items"] == []
    assert client.get("/api/ai/proposals", params={"project_id": second_project}).json()["total"] == 4
    assert client.get("/api/ai/proposals", params={"project_id": other["project_id"]}).status_code == 404
    for invalid in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert client.get("/api/ai/proposals", params={**params, **invalid}).status_code == 422


def test_proposal_pagination_does_not_expose_history_or_totals_after_role_revocation(paged_platform):
    client, app, owner, _ = paged_platform
    seed_history(app, owner, 2)
    params = {"project_id": owner["project_id"], "limit": 1, "offset": 1}
    assert client.get("/api/ai/proposals", params=params).status_code == 200
    with app.state.db.transaction(owner["tenant_id"], write=True) as session:
        member = session.scalar(select(Membership).where(
            Membership.project_id == owner["project_id"], Membership.user_id == owner["user_id"],
        ))
        member.role = "operator"
    response = client.get("/api/ai/proposals", params=params)
    assert response.status_code == 403
    assert "items" not in response.json() and "total" not in response.json()
