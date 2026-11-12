"""Real PostgreSQL tests; explicit admin/runtime URLs are required.

Each run creates and drops its own ontology_test_<uuid> database. No development
tenant or user data is modified. The application uses a NOBYPASSRLS login.
"""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from ontology.audit.models import Audit
from ontology.content_access import models as content_access_models  # noqa: F401
from ontology.core.config import Settings
from ontology.core.db import Database
from ontology.core.errors import DomainError
from ontology.identity.models import LoginSession, Project, User
from ontology.identity.security import token_hash
from ontology.integrations.feishu import models as feishu_models  # noqa: F401
from ontology.jobs.models import Job
from ontology.jobs.service import claim_for_tenant, finish
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def test_database_rls_without_application_filters(pg_platform):
    _, app, a, b = pg_platform
    db = app.state.db
    with db.transaction() as session:
        role = session.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
        assert role == (False, False)
        assert session.scalars(select(Project)).all() == []
        assert session.scalars(select(User)).all() == []
        assert session.scalars(select(LoginSession)).all() == []
    with db.transaction(a["tenant_id"]) as session:
        assert [p.id for p in session.scalars(select(Project))] == [a["project_id"]]
        assert session.get(Project, b["project_id"]) is None
        assert (
            session.execute(
                text("UPDATE projects SET name = 'escaped' WHERE id = :id"), {"id": b["project_id"]}
            ).rowcount
            == 0
        )
    with pytest.raises(DBAPIError), db.transaction(a["tenant_id"]) as session:
        session.add(Project(tenant_id=b["tenant_id"], name="forbidden"))
        session.flush()
    # A pooled connection must not retain the previous request's context.
    with db.transaction() as session:
        assert session.scalars(select(Project)).all() == []


def test_session_resolution_is_token_scoped_and_history_is_immutable(pg_platform):
    client, app, a, _ = pg_platform
    db = app.state.db
    digest = token_hash(client.cookies.get("ontology_session"))
    with db.transaction(session_hash=digest) as session:
        rows = session.scalars(select(LoginSession)).all()
        assert len(rows) == 1 and rows[0].token_hash == digest
        assert session.scalars(select(User)).all() == []
    with pytest.raises(DBAPIError), db.transaction(a["tenant_id"]) as session:
        session.execute(text("DELETE FROM audit_events"))
    with db.transaction(a["tenant_id"]) as session:
        assert session.scalar(select(Audit.id)) is not None


def test_runtime_provisioning_revokes_stale_ai_evidence_permissions(pg_databases):
    from ontology.core.registry import grant_runtime_access
    from sqlalchemy.engine import make_url

    admin, runtime_url = pg_databases
    role_name = make_url(runtime_url).username
    role = admin.engine.dialect.identifier_preparer.quote(role_name)
    with admin.engine.begin() as connection:
        # An earlier deployment gave this evidence table mutable-table permissions.
        connection.execute(text(f"GRANT UPDATE, DELETE ON ai_document_pages TO {role}"))
        assert connection.scalar(
            text("SELECT has_table_privilege(:role, 'ai_document_pages', 'UPDATE')"), {"role": role_name}
        )
        grant_runtime_access(connection, role_name)
        for permission in ("UPDATE", "DELETE"):
            assert not connection.scalar(
                text("SELECT has_table_privilege(:role, 'ai_document_pages', :permission)"),
                {"role": role_name, "permission": permission},
            )
        for permission in ("SELECT", "INSERT"):
            assert connection.scalar(
                text("SELECT has_table_privilege(:role, 'ai_document_pages', :permission)"),
                {"role": role_name, "permission": permission},
            )
        assert connection.scalar(
            text("SELECT has_table_privilege(:role, 'ai_proposals', 'UPDATE')"), {"role": role_name}
        )


def test_postgres_publish_and_concurrent_object_cas(pg_platform, definition):
    client, _, a, _ = pg_platform
    publish(client, a["project_id"], definition)
    response = client.post(
        "/api/objects",
        json={
            "project_id": a["project_id"],
            "type_key": "Project",
            "properties": {"name": "original", "budget": "123.45"},
        },
    )
    assert response.status_code == 201, response.text
    obj = response.json()

    def edit(name):
        return client.patch(
            f"/api/objects/{obj['id']}", json={"expected_revision": 1, "properties": {"name": name}}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["first", "second"]))
    assert sorted(r.status_code for r in results) == [200, 409]
    query = client.post(
        "/api/query",
        json={
            "project_id": a["project_id"],
            "type_key": "Project",
            "filters": [{"field": "budget", "op": "gt", "value": "123"}],
        },
    )
    assert query.status_code == 200, query.text
    assert query.json()["total"] == 1


def test_postgres_job_reclaim_fences_old_result(pg_platform):
    _, app, a, _ = pg_platform
    db = app.state.db
    with db.transaction(a["tenant_id"]) as session:
        job = Job(
            tenant_id=a["tenant_id"],
            project_id=a["project_id"],
            kind="document.parse",
            owner_id=a["user_id"],
            payload={},
        )
        session.add(job)
    old = claim_for_tenant(db, a["tenant_id"], "dead-worker", 300)
    with db.transaction(a["tenant_id"]) as session:
        session.get(Job, old["id"]).lease_until = time.time() - 1
    new = claim_for_tenant(db, a["tenant_id"], "replacement", 300)
    assert new["fencing_token"] == old["fencing_token"] + 1
    with pytest.raises(DomainError) as rejected:
        finish(db, old, result={"stale": True})
    assert rejected.value.status == 409
    finish(db, new, result={"fresh": True})
    with db.transaction(a["tenant_id"]) as session:
        row = session.get(Job, old["id"])
        assert row.status == "succeeded" and row.result == {"fresh": True}


def test_published_mapping_snapshot_runs_under_postgres_rls(pg_platform):
    from tests.support.ingestion import capture, make_source

    client, app, a, _ = pg_platform
    source, resource, _, _ = make_source(client, app, a["project_id"])
    result = capture(client, app, source, resource)
    assert result["snapshot"]["status"] == "complete", result
    assert result["snapshot"]["projection"]["created"] == 2
    with pytest.raises(DBAPIError), app.state.db.transaction(a["tenant_id"]) as session:
        session.execute(
            text("DELETE FROM source_record_revisions WHERE snapshot_id=:id"),
            {"id": result["snapshot"]["id"]},
        )


def test_production_rejects_administrative_database_credentials(pg_databases, tmp_path):
    from cryptography.fernet import Fernet

    admin, runtime_url = pg_databases
    options = {
        "environment": "production",
        "secure_cookies": True,
        "encryption_key": Fernet.generate_key().decode(),
        "data_dir": tmp_path,
    }
    with pytest.raises(ValueError, match="restricted runtime role"):
        Database(Settings(database_url=admin.engine.url.render_as_string(hide_password=False), **options))
    runtime = Database(Settings(database_url=runtime_url, **options))
    runtime.engine.dispose()


def test_query_preserves_tiny_decimals_and_large_integer_order(pg_platform):
    client, _, a, _ = pg_platform
    definition = {
        "types": [
            {
                "key": "Measurement",
                "label": "测量",
                "properties": [
                    {"key": "amount", "label": "数值", "data_type": "number"},
                    {"key": "count", "label": "计数", "data_type": "integer"},
                ],
            }
        ]
    }
    publish(client, a["project_id"], definition)
    records = [
        ("0.000000000000123456789012345678901", 9223372036854775809),
        ("0.000000000000123456789012345678902", 9223372036854775810),
    ]
    ids = []
    for amount, count in records:
        response = client.post(
            "/api/objects",
            json={
                "project_id": a["project_id"],
                "type_key": "Measurement",
                "properties": {"amount": amount, "count": count},
            },
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    for field, value in [("amount", records[0][0]), ("count", records[0][1])]:
        response = client.post(
            "/api/query",
            json={
                "project_id": a["project_id"],
                "type_key": "Measurement",
                "filters": [{"field": field, "op": "gt", "value": value}],
                "sort_field": field,
            },
        )
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [ids[1]]
