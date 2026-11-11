"""Exercise file-to-graph and local document reading without optional SDKs or models."""

import importlib.util
import io
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from ontology.api import create_app
from ontology.cli import bootstrap
from ontology.core.config import Settings
from ontology.worker import process_one
from PIL import Image, ImageDraw


def document_samples():
    image = Image.new("RGB", (480, 320), "white")
    ImageDraw.Draw(image).text((32, 32), "Synthetic core document", fill="black")
    png, pdf = io.BytesIO(), io.BytesIO()
    image.save(png, format="PNG")
    second = Image.new("RGB", (480, 320), "#18382f")
    image.save(pdf, format="PDF", save_all=True, append_images=[second], resolution=100)
    image.close()
    second.close()
    return [("synthetic.png", png.getvalue(), 1), ("synthetic.pdf", pdf.getvalue(), 2)]


def main():
    for module in (
        "pymysql",
        "pymongo",
        "neo4j",
        "rdflib",
        "lark_oapi",
        "docling",
        "pypdf",
        "paddleocr",
        "rapidocr",
        "torch",
    ):
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
            assert client.get("/api/model-connections").json()["items"] == []
            for filename, content, count in document_samples():
                document = post(
                    "/documents/upload",
                    data={"project_id": owner["project_id"], "engine": "basic"},
                    files={"file": (filename, content)},
                )["document"]
                for _ in range(10):
                    if not process_one(app.state.db, settings):
                        break
                result = client.get(f"/api/documents/{document['id']}")
                assert result.is_success, result.text
                parsed = result.json()
                assert parsed["parse_revision"] == 1 and parsed["current_parse_id"], parsed
                # These are image-only fixtures: native reading must not invent recognized text.
                assert parsed["state"] == "partial" and not parsed["ir"]["complete"], parsed
                assert len(parsed["ir"]["pages"]) == count, parsed
                response = client.get(
                    f"/api/documents/{document['id']}/pages", params={"limit": 1, "offset": count - 1}
                )
                assert response.is_success, response.text
                pages = response.json()
                assert pages["total"] == count and len(pages["items"]) == 1, pages
                page = pages["items"][0]
                assert page["page_number"] == count and page["has_image"], page
                rendered = client.get(page["image_url"])
                assert rendered.is_success and rendered.headers["content-type"] == "image/png", (
                    rendered.text[:200] if not rendered.is_success else ""
                )
                with Image.open(io.BytesIO(rendered.content)) as preview:
                    preview.load()
                    assert preview.width > 0 and preview.height > 0
            jobs = client.get("/api/jobs", params={"project_id": owner["project_id"]}).json()["items"]
            parses = [job for job in jobs if job["kind"] == "document.parse"]
            assert len(parses) == 2 and all(job["status"] == "succeeded" for job in parses), parses
            assert not any(job["kind"] == "ai.propose" for job in jobs)
        app.state.db.engine.dispose()
    print("Core-only upload, modeling, update, relation rule and graph checks passed.")
    print(
        "Synthetic PNG/PDF upload, native parse, pagination and local rendering passed without OCR or model calls."
    )


if __name__ == "__main__":
    main()
