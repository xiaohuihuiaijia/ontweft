"""Serve an isolated synthetic graph workload; never connect to developer databases.

Model publication uses the real API. Bulk ORM insertion creates load fixtures,
not proof that imports/audit/history are production-ready. All timings are local.
"""
import contextvars
import os
import time
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient
from ontology.objects.models import Entity, Relation
from sqlalchemy import event, insert
from start_local_demo import create_demo_app


def attach_request_metrics(app):
    request_metrics = contextvars.ContextVar("synthetic_graph_metrics", default=None)

    @event.listens_for(app.state.db.engine, "before_cursor_execute")
    def count_sql(_connection, _cursor, _statement, _parameters, _context, _many):
        metrics = request_metrics.get()
        if metrics is not None:
            metrics["queries"] += 1

    @app.middleware("http")
    async def local_metrics(request, call_next):
        # Test-only headers, never installed by the production app factory.
        metrics = {"queries": 0}
        token = request_metrics.set(metrics)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Synthetic-Sql-Count"] = str(metrics["queries"])
            response.headers["X-Synthetic-Service-Ms"] = str(round((time.perf_counter() - started) * 1000, 2))
            return response
        finally:
            request_metrics.reset(token)


def main():
    data = Path(os.environ["ONTOLOGY_PREVIEW_DATA_DIR"]).resolve()
    port = int(os.environ["ONTOLOGY_PREVIEW_PORT"])
    password = os.environ["ONTOLOGY_PREVIEW_PASSWORD"]
    app = create_demo_app(data, port=port, password=password)
    attach_request_metrics(app)
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"username": "owner", "password": password})
        response.raise_for_status()
        identity = client.get("/api/auth/me").json()
        project = identity["projects"][0]["id"]
        tenant = identity["tenant"]["id"]
        definition = {"types": [{
            "key": "asset", "label": "合成资产", "properties": [
                {"key": "name", "label": "资产名称", "data_type": "string", "required": True},
                {"key": "status", "label": "状态", "data_type": "enum", "enum": ["在用", "维护中"]},
                {"key": "private_note", "label": "受限字段", "data_type": "string", "read_roles": ["owner"]},
            ], "relations": [
                {"key": "depends_on", "label": "依赖", "target_type": "asset", "cardinality": "many"},
                {"key": "supplies", "label": "供应", "target_type": "asset", "cardinality": "many"},
                {"key": "self_review", "label": "自检", "target_type": "asset", "cardinality": "many"},
            ],
        }]}
        created = client.post("/api/changesets", json={"project_id": project, "title": "合成规模样例",
                              "definition": definition, "expected_baseline_release_id": None})
        created.raise_for_status()
        draft = created.json()
        for action in ("validate", "approve", "publish"):
            response = client.post(f"/api/changesets/{draft['id']}/{action}",
                                   json={"expected_revision": draft["revision"]})
            response.raise_for_status()
            draft = response.json()
        baseline = client.get("/api/ontology/definition", params={"project_id": project}).json()
    with app.state.db.transaction(tenant) as session:
        session.execute(insert(Entity), [{"id": f"scale-{i:04d}", "tenant_id": tenant,
            "project_id": project, "type_key": "asset", "release_id": baseline["release_id"],
            "properties": {"name": f"合成资产 {i:04d}", "status": "在用" if i % 3 else "维护中",
                           "private_note": "SYNTHETIC-OWNER-ONLY"}}
            for i in range(501)])
        relations = []
        for i in range(500):
            for suffix, kind, target in (("a", "depends_on", (i + 1) % 500),
                                         ("b", "supplies", (i + 499) % 500)):
                relations.append({"id": f"rel-{i:04d}-{suffix}", "tenant_id": tenant,
                    "project_id": project, "type_key": kind, "source_id": f"scale-{i:04d}",
                    "target_id": f"scale-{target:04d}"})
            if i % 5 == 0:
                relations.append({"id": f"rel-{i:04d}-c", "tenant_id": tenant, "project_id": project,
                    "type_key": "self_review", "source_id": f"scale-{i:04d}", "target_id": f"scale-{i:04d}"})
        session.execute(insert(Relation), relations)

    print("Synthetic workload ready: 501 nodes, 1100 relations; loopback SQLite only.", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)


if __name__ == "__main__":
    main()
