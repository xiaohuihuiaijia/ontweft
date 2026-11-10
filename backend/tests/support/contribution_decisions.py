"""API adjudication over real isolated ledgers, not live source collection.

Fixtures persist immutable raw records and exercise the production contribution
aggregator. PostgreSQL cases require the existing explicit admin/runtime URLs;
they create isolated databases and never use the development tenant.
"""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from ontology.audit.models import Audit
from ontology.modeling.models import Release
from ontology.objects.contributions.decision_models import ContributionDecision
from ontology.objects.contributions.models import ContributionRevision, SourceContribution
from ontology.objects.contributions.service import ensure_legacy_contributions
from ontology.objects.models import Entity, EntityRevision
from ontology.search.objects.models import ObjectSearchRequest
from ontology.sources.models import Source, SourceIdentity, SourceRecordRevision
from sqlalchemy import select

from tests.support.contributions import apply, loaded, raw_record, seed
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.fixture(params=["platform", "pg_platform"])
def decision_platform(request):
    return request.getfixturevalue(request.param)


def conflict_fixture(app, owner, *, quantity="12"):
    ids = seed(app.state.db, owner)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        ctx, entity, identity, release = loaded(session, owner, ids)
        ensure_legacy_contributions(session, ctx, entity)
        record = raw_record(
            session,
            owner,
            session.get(Source, ids["sources"][1]),
            release,
            {"name": "A", "quantity": quantity},
        )
        second = SourceIdentity(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            source_id=ids["sources"][1],
            resource_id="inventory",
            type_key="Stock",
            entity_id=entity.id,
            key_hash=record.key_hash,
            last_record_id=record.id,
            mapping_key="mapping1",
        )
        session.add(second)
        session.flush()
        apply(session, ctx, second, record, release, index=1, values=record.values, new_identity=True)
        rows = list(
            session.scalars(
                select(SourceContribution).where(SourceContribution.identity_id.in_([identity.id, second.id]))
            )
        )
        by_identity = {row.identity_id: row.id for row in rows}
        ids["identities"] = [identity.id, second.id]
        ids["contributions"] = [by_identity[identity.id], by_identity[second.id]]
        ids["records"] = [ids["record"], record.id]
    return ids


def path(ids):
    return f"/api/objects/{ids['entity']}/contribution-decisions"


def context(client, ids):
    response = client.get(path(ids))
    assert response.status_code == 200, response.text
    result = response.json()
    assert isinstance(result["expected_revision"], int)
    assert result["expected_model_release_id"]
    assert len(result["expected_contribution_fingerprint"]) == 64
    assert isinstance(result["fields"], list)
    return result


def payload(
    client, ids, *, operation="select", field="quantity", selected=1, reason="核对原始记录后采用该来源"
):
    current = context(client, ids)
    return {
        key: current[key]
        for key in ("expected_revision", "expected_model_release_id", "expected_contribution_fingerprint")
    } | {
        "field_key": field,
        "operation": operation,
        "selected_contribution_id": ids["contributions"][selected] if operation == "select" else None,
        "reason": reason,
    }


def preview(client, ids, body=None, **options):
    body = payload(client, ids, **options) if body is None else body
    response = client.post(path(ids) + "/preview", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["preview_fingerprint"]) == 64
    return body | {"expected_preview_fingerprint": result["preview_fingerprint"]}, result


def decide(client, ids, **options):
    body, _ = preview(client, ids, **options)
    response = client.post(path(ids), json=body)
    assert response.status_code in {200, 201}, response.text
    result = response.json()
    assert result["object"]["id"] == ids["entity"]
    return result


def ledger_snapshot(app, owner):
    """Include every adjudication side effect, not just the object's value."""
    models = (
        Entity,
        EntityRevision,
        SourceIdentity,
        SourceRecordRevision,
        SourceContribution,
        ContributionRevision,
        ContributionDecision,
        Audit,
        ObjectSearchRequest,
    )
    with app.state.db.transaction(owner["tenant_id"]) as session:
        return {
            model.__tablename__: [
                deepcopy(dict(row))
                for row in session.execute(
                    select(model.__table__)
                    .where(model.tenant_id == owner["tenant_id"])
                    .order_by(model.__table__.c.id)
                ).mappings()
            ]
            for model in models
        }


def source_version(app, owner, ids, index, *, values=None, withdraw=False):
    """A genuinely new immutable record, even when mapped values are equal."""
    with app.state.db.transaction(owner["tenant_id"]) as session:
        ctx, _, _, _ = loaded(session, owner, ids)
        release = session.get(Release, ctx.project.active_release_id)
        identity = session.get(SourceIdentity, ids["identities"][index])
        contribution = session.get(SourceContribution, ids["contributions"][index])
        if values is None:
            values = deepcopy(contribution.mapped_values)
        record = raw_record(
            session,
            owner,
            session.get(Source, ids["sources"][index]),
            release,
            values,
            generation=contribution.revision + 1,
        )
        kwargs = {"withdraw_reason": "来源明确撤回"} if withdraw else {"values": values}
        apply(session, ctx, identity, record, release, index=index, **kwargs)
        return record.id


def assert_suspended(app, owner, ids):
    with app.state.db.transaction(owner["tenant_id"]) as session:
        entity = session.get(Entity, ids["entity"])
        assert entity.data_status == "conflict"
        assert "quantity" not in entity.properties
        assert entity.properties["note"] == "manual retained"
        assert all(
            "quantity" not in ref.get("fields", []) for ref in entity.evidence if "source_record_id" in ref
        )


def role_client(client, app, owner, role):
    email = f"decision-{role}@example.test"
    password = "Decision-Role-Password"
    created = client.post("/api/users", json={"email": email, "display_name": role, "password": password})
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    added = client.post(
        "/api/members", json={"project_id": owner["project_id"], "user_id": user_id, "role": role}
    )
    assert added.status_code == 200, added.text
    slug = client.get("/api/auth/me").json()["tenant"]["slug"]
    reader = TestClient(app)
    response = reader.post(
        "/api/auth/login", json={"tenant_slug": slug, "email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return reader, user_id
