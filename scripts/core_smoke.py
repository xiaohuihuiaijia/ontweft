"""Exercise the file-to-graph path in an installation without optional SDKs."""

import importlib.util
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from ontology.api import create_app
from ontology.cli import bootstrap
from ontology.core.config import Settings
from ontology.worker import process_one


def main():
    for module in ("pymysql", "pymongo", "neo4j", "rdflib", "lark_oapi", "docling", "pypdf"):
        assert importlib.util.find_spec(module) is None, f"Core check requires {module} to be absent"
    with tempfile.TemporaryDirectory(prefix="ontweft-core-") as temporary:
        root = Path(temporary)
        settings = Settings(
            _env_file=None, database_url=f"sqlite:///{root / 'core.db'}", data_dir=root / "data"
        )
        app = create_app(settings)
        owner = bootstrap(
            app.state.db,
            tenant_slug="core",
            tenant_name="Core smoke",
            email="test@example.test",
            password="Core-Smoke-Password",
        )
        with TestClient(app) as client:

            def post(path, **kwargs):
                response = client.post("/api" + path, **kwargs)
                assert response.is_success, (path, response.status_code, response.text)
                return response.json()

            post(
                "/auth/login",
                json={"tenant_slug": "core", "email": "test@example.test", "password": "Core-Smoke-Password"},
            )
            assert client.get("/api/auth/me").json()["features"] == []
            source = post(
                "/sources/upload",
                data={"project_id": owner["project_id"]},
                files={"file": ("data.csv", b"code,name,parent\n001,Root,001\n002,Leaf,001\n")},
            )
            source = post(
                f"/sources/{source['id']}/file-identity",
                json={"expected_revision": source["revision"], "business_keys": {"sheet_1": ["c1"]}},
            )
            definition = post(
                f"/sources/{source['id']}/model-proposal",
                json={"expected_source_revision": source["revision"], "resource_ids": ["sheet_1"]},
            )["definition"]
            kind = definition["types"][0]
            kind["unique_keys"] = [["c1"]]
            kind["relations"] = [
                {"key": "parent", "label": "Parent", "target_type": kind["key"], "cardinality": "one"}
            ]
            change = post(
                "/changesets",
                json={"project_id": owner["project_id"], "title": "Core model", "definition": definition},
            )
            for operation in ("validate", "approve", "publish"):
                change = post(
                    f"/changesets/{change['id']}/{operation}", json={"expected_revision": change["revision"]}
                )
            post(
                f"/sources/{source['id']}/snapshots",
                json={"expected_source_revision": source["revision"], "resource_id": "sheet_1"},
            )
            assert process_one(app.state.db, settings)
            rule = {
                "project_id": owner["project_id"],
                "expected_model_release_id": change["release_id"],
                "source_type": kind["key"],
                "relation_key": "parent",
                "source_field": "c3",
                "target_field": "c1",
                "allow_self": True,
            }
            post("/relation-rules", json={"name": "Core parent rule", "rule": rule})
            for _ in range(10):
                if not process_one(app.state.db, settings):
                    break
            graph = client.get("/api/graph", params={"project_id": owner["project_id"]}).json()
            assert len(graph["nodes"]) == 2 and len(graph["edges"]) == 2, graph
            version = post(
                f"/sources/{source['id']}/versions",
                data={"expected_revision": source["revision"]},
                files={"file": ("updated.csv", b"code,name,parent\n002,Leaf updated,001\n001,Root,001\n")},
            )
            post(
                f"/sources/{source['id']}/versions/{version['id']}/apply",
                json={"expected_revision": source["revision"]},
            )
            for _ in range(10):
                if not process_one(app.state.db, settings):
                    break
            updated = client.get("/api/graph", params={"project_id": owner["project_id"]}).json()
            assert {n["id"] for n in graph["nodes"]} == {n["id"] for n in updated["nodes"]}
            assert len(updated["edges"]) == 2
        app.state.db.engine.dispose()
    print("Core-only upload, modeling, update, relation rule and graph checks passed.")


if __name__ == "__main__":
    main()
