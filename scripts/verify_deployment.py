"""Exercise the real Linux Compose image and backup/restore using synthetic data."""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]


def checked(response):
    response.raise_for_status()
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scale",
        action="store_true",
        help="Restore 100k synthetic objects and 1m relations; one real uploaded asset",
    )
    args = parser.parse_args()
    name = "ontweft-beta-" + uuid4().hex[:10]
    reserve = socket.socket()
    reserve.bind(("127.0.0.1", 0))
    port = reserve.getsockname()[1]
    reserve.close()
    output = ROOT / "var" / "reports" / name
    output.mkdir(parents=True)
    env = {
        **os.environ,
        "COMPOSE_PROJECT_NAME": name,
        "COMPOSE_FILE": str(ROOT / "compose.yaml"),
        "COMPOSE_DISABLE_ENV_FILE": "1",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "RUNTIME_PASSWORD": secrets.token_urlsafe(24),
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "ALLOWED_ORIGINS": '["https://synthetic.example.test"]',
        "PORT": str(port),
        "ONTWEFT_IMAGE": os.environ.get("ONTOLOGY_TEST_IMAGE", "ontweft:beta-test"),
    }
    password = "Synthetic-Deployment-Password"
    report = {"fixture": "isolated Compose project; synthetic records only", "passed": False}
    log = (output / "compose.log").open("w", encoding="utf-8")

    def compose(*args):
        return subprocess.run(
            ["docker", "compose", *args], cwd=ROOT, env=env, check=True, stdout=log, stderr=subprocess.STDOUT
        )

    def wait_health():
        for _ in range(120):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise AssertionError("Isolated deployment did not become healthy")

    def login(email="owner@example.test"):
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}/api", timeout=30)
        response = client.post(
            "/auth/login", json={"tenant_slug": "deploy-check", "email": email, "password": password}
        )
        checked(response)
        assert "Secure" in response.headers["set-cookie"]
        # Exercise the secure-cookie deployment over its local container-test
        # transport. Production browsers require the documented HTTPS proxy.
        client.headers["Cookie"] = "ontology_session=" + response.cookies["ontology_session"]
        return client

    try:
        compose("up", "-d", "--no-build")
        wait_health()
        compose(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python",
            "migrate",
            "-c",
            "from ontology.cli import bootstrap; from ontology.core.config import Settings; from ontology.core.db import Database; "
            "db=Database(Settings()); bootstrap(db,tenant_slug='deploy-check',tenant_name='Synthetic deployment',email='owner@example.test',password='Synthetic-Deployment-Password'); db.engine.dispose()",
        )
        with closing(login()) as client:
            me = checked(client.get("/auth/me"))
            project = me["projects"][0]["id"]
            assert me["features"] == [] and me["model_approval"] == "separate"
            user = checked(
                client.post(
                    "/users",
                    json={
                        "email": "reviewer@example.test",
                        "display_name": "Synthetic reviewer",
                        "password": password,
                    },
                )
            )
            checked(
                client.post(
                    "/members", json={"project_id": project, "user_id": user["id"], "role": "reviewer"}
                )
            )
            source = checked(
                client.post(
                    "/sources/upload",
                    data={"project_id": project},
                    files={"file": ("retained.csv", b"code,name\n001,Retained original\n")},
                )
            )
            source = checked(
                client.post(
                    f"/sources/{source['id']}/file-identity",
                    json={"expected_revision": source["revision"], "business_keys": {"sheet_1": ["c1"]}},
                )
            )
            definition = checked(
                client.post(
                    f"/sources/{source['id']}/model-proposal",
                    json={"expected_source_revision": source["revision"], "resource_ids": ["sheet_1"]},
                )
            )["definition"]
            if args.scale:
                kind = definition["types"][0]
                kind["relations"] = [
                    {"key": "linked", "label": "Linked", "target_type": kind["key"], "cardinality": "many"}
                ]
            change = checked(
                client.post(
                    "/changesets",
                    json={
                        "project_id": project,
                        "title": "Deployment verification",
                        "definition": definition,
                    },
                )
            )
            change = checked(
                client.post(
                    f"/changesets/{change['id']}/validate", json={"expected_revision": change["revision"]}
                )
            )
            assert (
                client.post(
                    f"/changesets/{change['id']}/approve", json={"expected_revision": change["revision"]}
                ).status_code
                == 403
            )
            with closing(login("reviewer@example.test")) as reviewer:
                change = checked(
                    reviewer.post(
                        f"/changesets/{change['id']}/approve", json={"expected_revision": change["revision"]}
                    )
                )
            published = checked(
                client.post(
                    f"/changesets/{change['id']}/publish", json={"expected_revision": change["revision"]}
                )
            )
            snapshot = checked(
                client.post(
                    f"/sources/{source['id']}/snapshots",
                    json={"expected_source_revision": source["revision"], "resource_id": "sheet_1"},
                )
            )["snapshot"]
            for _ in range(120):
                state = checked(client.get(f"/snapshots/{snapshot['id']}/records"))["snapshot"]
                if state["status"] in {"complete", "failed"}:
                    break
                time.sleep(0.25)
            assert state["status"] == "complete", state
            before = checked(client.get("/objects", params={"project_id": project}))
            assert before["total"] == 1
            metrics = checked(client.get("/jobs/metrics", params={"project_id": project}))
            assert metrics["failed"] == 0 and metrics["worker_heartbeat_age_seconds"] is not None
            report["metrics"] = metrics
            versions = checked(client.get(f"/sources/{source['id']}/versions"))["items"]
            original_path = f"/sources/{source['id']}/versions/{versions[0]['id']}/original"
            original = client.get(original_path).content
            if args.scale:
                # Seed ONLY this tool's isolated synthetic project. This measures
                # backup/restore volume, not API ingestion or initial indexing.
                params = {
                    "tenant": me["tenant"]["id"],
                    "project": project,
                    "release": published["release_id"],
                    "kind": definition["types"][0]["key"],
                    "properties": json.dumps(before["items"][0]["properties"]),
                }
                code = f"""
from ontology.core.config import Settings
from ontology.core.db import Database
from sqlalchemy import text
db = Database(Settings())
p = {params!r}
with db.transaction(p['tenant']) as s:
    s.execute(text('''INSERT INTO entities(id,tenant_id,project_id,created_at,updated_at,revision,type_key,properties,evidence,release_id,deleted,data_status,changed_seq)
        SELECT md5('scale-entity-'||i)::uuid::text,:tenant,:project,'2026-11-10T00:00:00+00:00','2026-11-10T00:00:00+00:00',1,:kind,
        CAST(:properties AS jsonb)||jsonb_build_object('c1','scale'||i,'name','Scale '||i),'[]',:release,false,'active',0
        FROM generate_series(1,99999) i'''), p)
    s.execute(text('''INSERT INTO relations(id,tenant_id,project_id,created_at,type_key,source_id,target_id,properties,evidence,revision,manual)
        SELECT md5('scale-relation-'||i)::uuid::text,:tenant,:project,'2026-11-10T00:00:00+00:00','linked',
        md5('scale-entity-'||(mod(i-1,99999)+1))::uuid::text,
        md5('scale-entity-'||(mod(mod(i-1,99999)+(i-1)/99999+1,99999)+1))::uuid::text,'{{}}','[]',1,true
        FROM generate_series(1,1000000) i'''), p)
db.engine.dispose()
"""
                compose("run", "--rm", "--no-deps", "--entrypoint", "python", "migrate", "-c", code)
                before = checked(client.get("/objects", params={"project_id": project}))
                assert before["total"] == 100000
        report["objects"] = before["total"]
        report["relations"] = 1000000 if args.scale else 0
        report["asset_scope"] = (
            "one actual immutable CSV and its normalized table; scale records are synthetic bulk fixtures"
        )
        started = time.monotonic()
        subprocess.run(
            [sys.executable, "deploy/backup.py", "create", str(output / "backup")],
            cwd=ROOT,
            env=env,
            check=True,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        report["backup_seconds"] = round(time.monotonic() - started, 2)
        # A separate project/database/volume proves restore without ever clearing
        # a source volume or attaching to an existing deployment.
        compose("stop", "api", "worker")
        env["COMPOSE_PROJECT_NAME"] = name + "-restore"
        compose("up", "-d", "--wait", "db")
        started = time.monotonic()
        subprocess.run(
            [sys.executable, "deploy/backup.py", "restore", str(output / "backup")],
            cwd=ROOT,
            env=env,
            check=True,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        compose("up", "-d", "--no-build")
        wait_health()
        with closing(login()) as client:
            restored = checked(client.get("/objects", params={"project_id": project}))
            assert restored == before
            assert client.get(original_path).content == original
        if args.scale:
            code = f"""
from ontology.core.config import Settings
from ontology.core.db import Database
from sqlalchemy import text
db = Database(Settings())
with db.transaction({me["tenant"]["id"]!r}) as s:
    assert s.scalar(text('SELECT count(*) FROM entities WHERE project_id=:project'), {{'project':{project!r}}}) == 100000
    assert s.scalar(text('SELECT count(*) FROM relations WHERE project_id=:project'), {{'project':{project!r}}}) == 1000000
db.engine.dispose()
"""
            compose("exec", "-T", "api", "python", "-c", code)
        report["restore_seconds"] = round(time.monotonic() - started, 2)
        report["passed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        # Delete only the two explicitly created synthetic projects and volumes.
        for project_name in (name + "-restore", name):
            env["COMPOSE_PROJECT_NAME"] = project_name
            subprocess.run(
                ["docker", "compose", "down", "--volumes", "--remove-orphans"],
                check=False,
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        log.close()
        (output / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
