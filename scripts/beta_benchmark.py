"""Synthetic PostgreSQL/API scale test. Never attaches to an application database.

Needs ONTOLOGY_TEST_ADMIN_URL and ONTOLOGY_TEST_RUNTIME_URL. Creates and drops a
uniquely named database. Bulk fixtures measure steady-state queries, not ingest
throughput; incremental propagation uses the real API and worker.
"""

import argparse
import hashlib
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from ontology.api import create_app
from ontology.cli import bootstrap
from ontology.core.config import Settings
from ontology.core.db import Database
from ontology.core.models import now
from ontology.identity.models import LoginSession, Membership, Project, User
from ontology.identity.security import token_hash
from ontology.modeling.models import ChangeSet, Release
from ontology.objects.rule_models import RelationRule
from ontology.search.objects.index import process_one_index
from ontology.search.objects.models import SearchProjectState
from ontology.search.tokens import TOKENIZER_VERSION
from ontology.worker import process_one
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url


def identifier(number):
    return str(__import__("uuid").UUID(hashlib.md5(f"entity-{number}".encode()).hexdigest()))


def checked(response):
    response.raise_for_status()
    return response.json()


def percentile(values, p):
    return round(sorted(values)[min(len(values) - 1, int(len(values) * p))], 3)


def stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        # Terminating only the Uvicorn supervisor leaves spawned workers on Windows.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objects", type=int, default=100000)
    parser.add_argument("--relations", type=int, default=1000000)
    parser.add_argument("--users", type=int, default=50)
    parser.add_argument("--requests-per-user", type=int, default=20)
    parser.add_argument(
        "--workers", type=int, default=1, help="Uvicorn worker processes in the same API service"
    )
    parser.add_argument("--output", type=Path, default=Path("var/beta-benchmark.json"))
    parser.add_argument(
        "--common-queries", action="store_true", help="Include a term matching every synthetic object"
    )
    parser.add_argument(
        "--concurrent-writes",
        action="store_true",
        help="Measure simultaneous writes through API and existing worker",
    )
    args = parser.parse_args()
    admin = make_url(os.environ["ONTOLOGY_TEST_ADMIN_URL"])
    runtime = make_url(os.environ["ONTOLOGY_TEST_RUNTIME_URL"])
    name = "ontology_beta_" + uuid4().hex
    provisioner = create_engine(admin, isolation_level="AUTOCOMMIT")
    with provisioner.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin_url = admin.set(database=name).render_as_string(hide_password=False)
    runtime_url = runtime.set(database=name).render_as_string(hide_password=False)
    data_dir = args.output.parent / name
    db = Database(Settings(_env_file=None, database_url=admin_url, data_dir=data_dir))
    server = None
    app = None
    report = {
        "objects": args.objects,
        "relations": args.relations,
        "concurrent_users": args.users,
        "fixture": "synthetic bulk objects, relations and prebuilt full-text projection; not ingest/backfill throughput",
        "application": f"{platform.system()}; {args.workers} separate uvicorn worker(s); PostgreSQL 17; pg8000; forced RLS runtime role",
        "measurements": {},
    }
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env={**os.environ, "ONTOLOGY_DATABASE_URL": admin_url},
            check=True,
            stdout=subprocess.DEVNULL,
        )
        owner = bootstrap(
            db,
            tenant_slug="scale-test",
            tenant_name="Synthetic benchmark",
            email="benchmark@example.test",
            password="Synthetic-Benchmark-Password",
        )
        role = db.engine.dialect.identifier_preparer.quote(runtime.username)
        with db.engine.begin() as conn:
            conn.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            conn.execute(
                text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")
            )
        scope = {"tenant_id": owner["tenant_id"], "project_id": owner["project_id"]}
        definition = {
            "types": [
                {
                    "key": "Asset",
                    "label": "资产",
                    "properties": [
                        {"key": "code", "label": "编码", "data_type": "string"},
                        {"key": "name", "label": "名称", "data_type": "string"},
                        {"key": "parent_code", "label": "上级编码", "data_type": "string"},
                    ],
                    "relations": [
                        {"key": "linked", "label": "关联", "target_type": "Asset", "cardinality": "many"},
                        {"key": "parent", "label": "归属", "target_type": "Asset", "cardinality": "one"},
                    ],
                }
            ]
        }
        cookies = []
        with db.transaction(owner["tenant_id"]) as session:
            change = ChangeSet(
                **scope,
                title="Synthetic benchmark fixture",
                definition=definition,
                author_id=owner["user_id"],
            )
            session.add(change)
            session.flush()
            release = Release(
                **scope,
                version=1,
                definition=definition,
                changeset_id=change.id,
                published_by=owner["user_id"],
            )
            session.add(release)
            session.flush()
            session.get(Project, owner["project_id"]).active_release_id = release.id
            session.get(Project, owner["project_id"]).version = 1
            release_id = release.id
            generation = str(uuid4())
            session.add(
                SearchProjectState(
                    **scope,
                    release_id=release.id,
                    generation=generation,
                    tokenizer_version=TOKENIZER_VERSION,
                    backfill_complete=True,
                )
            )
            rule = RelationRule(
                **scope,
                name="Synthetic parent rule",
                owner_id=owner["user_id"],
                indexed=True,
                config={
                    "project_id": owner["project_id"],
                    "expected_model_release_id": release.id,
                    "source_type": "Asset",
                    "relation_key": "parent",
                    "source_field": "parent_code",
                    "target_field": "code",
                    "allow_self": False,
                },
            )
            session.add(rule)
            session.flush()
            rule_id = rule.id
            for i in range(args.users):
                if i == 0:
                    user_id = owner["user_id"]
                else:
                    user = User(
                        tenant_id=owner["tenant_id"],
                        email=f"load-{i}@example.test",
                        display_name=f"Load {i}",
                        password_hash="disabled-synthetic-login",
                    )
                    session.add(user)
                    session.flush()
                    user_id = user.id
                    session.add(
                        Membership(
                            **scope,
                            user_id=user_id,
                            role="operator" if args.concurrent_writes else "member",
                        )
                    )
                token = secrets.token_urlsafe(32)
                cookies.append(token)
                session.add(
                    LoginSession(
                        token_hash=token_hash(token),
                        tenant_id=owner["tenant_id"],
                        user_id=user_id,
                        user_revision=1,
                        expires_at=time.time() + 7200,
                    )
                )
        started = time.perf_counter()
        with db.transaction(owner["tenant_id"]) as session:
            params = {
                **scope,
                "release": release_id,
                "n": args.objects,
                "m": args.relations,
                "created": now(),
                "generation": generation,
                "tokenizer": TOKENIZER_VERSION,
                "rule": rule_id,
            }
            session.execute(
                text("""INSERT INTO entities(id,tenant_id,project_id,created_at,updated_at,type_key,properties,evidence,revision,release_id,deleted,data_status,changed_seq)
                SELECT md5('entity-'||i)::uuid::text,:tenant_id,:project_id,:created,:created,'Asset',
                    json_build_object('code','item'||i,'name','Synthetic '||i,'parent_code',NULL),'[]'::json,1,:release,false,'active',0
                FROM generate_series(1,:n) i"""),
                params,
            )
            session.execute(
                text("""INSERT INTO relations(id,tenant_id,project_id,created_at,type_key,source_id,target_id,properties,evidence,revision,manual)
                SELECT md5('edge-'||i)::uuid::text,:tenant_id,:project_id,:created,'linked',
                    md5('entity-'||(mod(i-1,:n)+1))::uuid::text,
                    md5('entity-'||(mod(mod(i-1,:n)+1+(i-1)/:n,:n)+1))::uuid::text,'{}'::json,'[]'::json,1,true
                FROM generate_series(1,:m) i"""),
                params,
            )
            session.execute(
                text("""INSERT INTO object_search_documents(id,tenant_id,project_id,created_at,entity_id,entity_revision,release_id,generation,tokenizer_version,status,indexed_at)
                SELECT md5('search-doc-'||id)::uuid::text,:tenant_id,:project_id,:created,id,1,:release,:generation,:tokenizer,'ready',:created FROM entities"""),
                params,
            )
            session.execute(
                text("""INSERT INTO object_search_fields(id,tenant_id,project_id,created_at,entity_id,field_key,chunk,token_text,generation)
                SELECT md5('search-field-'||id)::uuid::text,:tenant_id,:project_id,:created,id,'code',0,' '||(properties->>'code')||' ',:generation FROM entities"""),
                params,
            )
            if args.common_queries:
                session.execute(
                    text("""INSERT INTO object_search_fields(id,tenant_id,project_id,created_at,entity_id,field_key,chunk,token_text,generation)
                    SELECT md5('search-common-'||id)::uuid::text,:tenant_id,:project_id,:created,id,'name',0,' synthetic ',:generation FROM entities"""),
                    params,
                )
            session.execute(
                text("""INSERT INTO relation_match_index(id,tenant_id,project_id,created_at,rule_id,entity_id,side,value_hash)
                SELECT md5('rule-source-'||id)::uuid::text,:tenant_id,:project_id,:created,:rule,id,'source',NULL FROM entities
                UNION ALL
                SELECT md5('rule-target-'||id)::uuid::text,:tenant_id,:project_id,:created,:rule,id,'target',
                  encode(sha256(convert_to('["text","'||(properties->>'code')||'"]','UTF8')),'hex') FROM entities"""),
                params,
            )
        with db.engine.begin() as conn:
            conn.execute(text("ANALYZE"))
        report["seed_seconds"] = round(time.perf_counter() - started, 2)
        print("Synthetic scale fixture ready", flush=True)
        settings = Settings(_env_file=None, database_url=runtime_url, data_dir=data_dir, extensions=[])
        app = create_app(settings)
        example_query = []
        page_queries = {}
        probe_statements = []

        def remember_query(_conn, _cursor, statement, parameters, _context, _many):
            probe_statements.append(statement.splitlines()[0])
            if "ORDER BY entities.updated_at" in statement and "object_search_documents" in statement:
                page_queries["common" if " synthetic " in str(parameters) else "unique"] = (
                    statement,
                    parameters,
                )
            if (
                not example_query
                and "count(*)" in statement
                and "object_search_documents" in statement
                and "object_search_fields" in statement
            ):
                example_query.append((statement, parameters))

        event.listen(app.state.db.engine, "before_cursor_execute", remember_query)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        # Keep load-generator threads out of the API interpreter/GIL. Use the
        # same actual entry point as deployment, without tuning its concurrency.
        server = subprocess.Popen(
            [sys.executable, "-m", "ontology.cli", "serve", "--port", str(port)],
            env={
                **os.environ,
                "ONTOLOGY_DATABASE_URL": runtime_url,
                "ONTOLOGY_DATA_DIR": str(data_dir),
                "ONTOLOGY_EXTENSIONS": "[]",
                "WEB_CONCURRENCY": str(args.workers),
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{port}/api"
        for _ in range(100):
            if server.poll() is not None:
                raise RuntimeError("Isolated API failed to start")
            try:
                if httpx.get(base + "/health", timeout=1).is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Isolated API startup timed out")
        # Capture a real authorized count query for EXPLAIN separately from
        # measured network requests. This does not replace any load request.
        with TestClient(app) as probe:
            probe.cookies.set("ontology_session", cookies[0])
            checked(
                probe.post(
                    "/api/search/objects",
                    json={
                        "project_id": owner["project_id"],
                        "expected_release_id": release_id,
                        "expected_model_version": 1,
                        "q": "item1",
                        "limit": 20,
                    },
                )
            )
            if args.common_queries:
                checked(
                    probe.post(
                        "/api/search/objects",
                        json={
                            "project_id": owner["project_id"],
                            "expected_release_id": release_id,
                            "expected_model_version": 1,
                            "q": "synthetic",
                            "limit": 20,
                            "count_total": False,
                        },
                    )
                )
        report["search_probe"] = {
            "scope": "one exact unique query plus optional bounded common query",
            "requests": 2 if args.common_queries else 1,
            "sql_statements": len(probe_statements),
            "timeout_setting_statements": sum("timeout" in statement for statement in probe_statements),
        }
        event.remove(app.state.db.engine, "before_cursor_execute", remember_query)
        barrier = threading.Barrier(args.users)

        def client_load(index):
            timings = {"fulltext": [], "neighbors": [], "object_detail": []}
            if args.common_queries:
                timings["fulltext_common"] = []
            errors = []
            with httpx.Client(
                base_url=base, cookies={"ontology_session": cookies[index]}, timeout=45
            ) as client:
                barrier.wait()
                # Record the first concurrent wave separately, then measure
                # steady operation. Do not silently discard cold-start latency.
                started = time.perf_counter()
                try:
                    result = checked(
                        client.post(
                            "/search/objects",
                            json={
                                "project_id": owner["project_id"],
                                "expected_release_id": release_id,
                                "expected_model_version": 1,
                                "q": "item" + str(index + 1),
                                "count_total": False,
                                "limit": 20,
                            },
                        )
                    )
                    if len(result["items"]) != 1:
                        errors.append("startup fulltext result mismatch")
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    errors.append(f"startup: {type(exc).__name__}")
                startup = time.perf_counter() - started
                barrier.wait(timeout=60)
                for j in range(args.requests_per_user):
                    value = ((index * 997 + j * 7919) % args.objects) + 1
                    kind = list(timings)[j % len(timings)]
                    started = time.perf_counter()
                    try:
                        if kind in {"fulltext", "fulltext_common"}:
                            response = client.post(
                                "/search/objects",
                                json={
                                    "project_id": owner["project_id"],
                                    "expected_release_id": release_id,
                                    "expected_model_version": 1,
                                    "q": "synthetic" if kind == "fulltext_common" else "item" + str(value),
                                    "count_total": False,
                                    "limit": 20,
                                },
                            )
                        elif kind == "neighbors":
                            response = client.get(
                                "/graph/neighbors",
                                params={
                                    "project_id": owner["project_id"],
                                    "object_id": identifier(value),
                                    "limit": 50,
                                },
                            )
                        else:
                            response = client.get("/objects/" + identifier(value))
                        result = checked(response)
                        if kind == "fulltext_common" and (
                            len(result["items"]) != min(20, args.objects)
                            or result["has_more"] != (args.objects > 20)
                        ):
                            errors.append("common fulltext result mismatch")
                        if kind == "fulltext" and len(result["items"]) != 1:
                            errors.append("fulltext result mismatch")
                    except httpx.HTTPStatusError as exc:
                        errors.append(f"{kind}: HTTP {exc.response.status_code} " + exc.response.text[:200])
                    except (httpx.HTTPError, ValueError, KeyError) as exc:
                        errors.append(f"{kind}: {type(exc).__name__}")
                    timings[kind].append(time.perf_counter() - started)
            return timings, errors, startup

        with ThreadPoolExecutor(max_workers=args.users) as pool:
            results = list(pool.map(client_load, range(args.users)))
        for kind in results[0][0]:
            values = [v for timings, _, _startup in results for v in timings[kind]]
            report["measurements"][kind] = {
                "requests": len(values),
                "p50_seconds": percentile(values, 0.5),
                "p95_seconds": percentile(values, 0.95),
                "max_seconds": round(max(values), 3),
            }
        report["errors"] = [e for _, errors, _startup in results for e in errors]
        report["first_wave"] = {
            "scope": "one simultaneous initial full-text request per user, before the measured steady requests",
            "requests": args.users,
            "p95_seconds": percentile([startup for _, _, startup in results], 0.95),
        }
        if example_query:
            with app.state.db.transaction(owner["tenant_id"]) as session:
                statement, parameters = example_query[0]
                report["fulltext_query_plan"] = [
                    row[0]
                    for row in session.connection().exec_driver_sql(
                        "EXPLAIN (ANALYZE, BUFFERS) " + statement, parameters
                    )
                ]
        report["interactive_search"] = (
            "UI requests count_total=false: one authorized page plus has_more; legacy API default keeps exact totals"
        )
        for label, (statement, parameters) in page_queries.items():
            with app.state.db.transaction(owner["tenant_id"]) as session:
                report[label + "_page_plan"] = [
                    row[0]
                    for row in session.connection().exec_driver_sql(
                        "EXPLAIN (ANALYZE, BUFFERS) " + statement, parameters
                    )
                ]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Concurrent API measurements completed", flush=True)
        # Limit rule backfill to a real small type rather than mistaking prebuilt
        # load fixtures for measured ingestion. All changes below use API/worker.
        with httpx.Client(base_url=base, cookies={"ontology_session": cookies[0]}, timeout=90) as client:
            started = time.perf_counter()
            change = checked(
                client.post(
                    "/changesets",
                    json={
                        "project_id": owner["project_id"],
                        "title": "Scale validation",
                        "expected_baseline_release_id": release_id,
                        "definition": definition,
                    },
                )
            )
            change = checked(
                client.post(
                    f"/changesets/{change['id']}/validate", json={"expected_revision": change["revision"]}
                )
            )
            count = 0
            while change["status"] == "validating":
                if not process_one(app.state.db, settings):
                    raise RuntimeError("Validation job did not progress")
                count += 1
                change = checked(client.get("/changesets", params={"project_id": owner["project_id"]}))[
                    "items"
                ][0]
            report["validation"] = {
                "seconds": round(time.perf_counter() - started, 2),
                "batches": count,
                "status": change["status"],
                "report": change["validation"],
            }
            propagation = []
            for i in range(1, 21):
                obj = checked(client.get("/objects/" + identifier(i)))
                marker = f"updateditem{i}"
                started = time.perf_counter()
                checked(
                    client.patch(
                        "/objects/" + obj["id"],
                        json={
                            "expected_revision": obj["revision"],
                            "properties": {"code": marker, "parent_code": f"item{1000 + i}"},
                        },
                    )
                )
                while True:
                    process_one(app.state.db, settings)
                    process_one_index(app.state.db, owner["tenant_id"])
                    found = checked(
                        client.post(
                            "/search/objects",
                            json={
                                "project_id": owner["project_id"],
                                "expected_release_id": release_id,
                                "expected_model_version": 1,
                                "q": marker,
                            },
                        )
                    )
                    graph = checked(
                        client.get(
                            "/graph/neighbors",
                            params={"project_id": owner["project_id"], "object_id": obj["id"], "limit": 100},
                        )
                    )
                    if found["total"] == 1 and any(
                        edge["type_key"] == "parent" and edge["target"] == identifier(1000 + i)
                        for edge in graph["edges"]
                    ):
                        break
                    if time.perf_counter() - started > 60:
                        raise RuntimeError("Incremental indexing did not converge")
                propagation.append(time.perf_counter() - started)
            report["propagation"] = {
                "scope": "API object edit to searchable result AND rule-maintained relation, real workers, 20 sequential updates; initial indexes are prebuilt fixture data",
                "p95_seconds": percentile(propagation, 0.95),
                "max_seconds": round(max(propagation), 3),
            }
        if args.concurrent_writes:
            # Use the same independent worker process as deployment. Client
            # polling must not share its interpreter or GIL with indexing.
            worker_log = (data_dir / "propagation-worker.log").open("w", encoding="utf-8")
            heartbeat = data_dir / "worker.heartbeat"
            heartbeat.unlink(missing_ok=True)
            worker_started = time.perf_counter()
            worker = subprocess.Popen(
                [sys.executable, "-m", "ontology.cli", "worker"],
                env={
                    **os.environ,
                    "ONTOLOGY_DATABASE_URL": runtime_url,
                    "ONTOLOGY_DATA_DIR": str(data_dir),
                    "ONTOLOGY_EXTENSIONS": "[]",
                },
                stdout=worker_log,
                stderr=subprocess.STDOUT,
            )
            accepted_writes, write_times, transient_reads = [], [], []
            write_barrier = threading.Barrier(args.users)

            def write_and_observe(index):
                with httpx.Client(
                    base_url=base, cookies={"ontology_session": cookies[index]}, timeout=90
                ) as client:
                    obj = checked(client.get("/objects/" + identifier(index + 1)))
                    write_barrier.wait(timeout=90)
                    start = time.perf_counter()
                    marker = "concurrentitem" + str(index)
                    checked(
                        client.patch(
                            "/objects/" + obj["id"],
                            json={
                                "expected_revision": obj["revision"],
                                "properties": {"code": marker, "parent_code": f"item{2000 + index}"},
                            },
                        )
                    )
                    accepted_writes.append(index)
                    write_times.append(time.perf_counter() - start)
                    while time.perf_counter() - start < 90:
                        response = client.post(
                            "/search/objects",
                            json={
                                "project_id": owner["project_id"],
                                "expected_release_id": release_id,
                                "expected_model_version": 1,
                                "q": marker,
                                "count_total": False,
                            },
                        )
                        if response.status_code == 503:
                            transient_reads.append(response.json()["error"]["code"])
                            time.sleep(1)
                            continue
                        found = checked(response)
                        if not found["items"]:
                            time.sleep(1)
                            continue
                        graph = checked(
                            client.get(
                                "/graph/neighbors",
                                params={
                                    "project_id": owner["project_id"],
                                    "object_id": obj["id"],
                                    "limit": 100,
                                },
                            )
                        )
                        if len(found["items"]) == 1 and any(
                            e["type_key"] == "parent" and e["target"] == identifier(2000 + index)
                            for e in graph["edges"]
                        ):
                            return time.perf_counter() - start
                        if worker.poll() is not None:
                            raise RuntimeError("Propagation worker stopped")
                        time.sleep(1)
                    raise RuntimeError("Propagation exceeded 90 seconds")

            try:
                while not heartbeat.exists():
                    if worker.poll() is not None or time.perf_counter() - worker_started > 90:
                        raise RuntimeError("Isolated worker startup did not complete")
                    time.sleep(0.1)
                worker_startup = round(time.perf_counter() - worker_started, 3)
                with ThreadPoolExecutor(max_workers=args.users) as pool:
                    futures = [pool.submit(write_and_observe, i) for i in range(args.users)]
                    elapsed, failures = [], []
                    for future in futures:
                        try:
                            elapsed.append(future.result())
                        except Exception as exc:
                            failures.append(
                                f"HTTP {exc.response.status_code} {exc.response.text[:300]}"
                                if isinstance(exc, httpx.HTTPStatusError)
                                else f"{type(exc).__name__}: {exc}"
                            )
                report["concurrent_propagation"] = {
                    "users": args.users,
                    "completed": len(elapsed),
                    "writes_accepted": len(accepted_writes),
                    "worker": "separate ontology worker process",
                    "worker_startup_seconds": worker_startup,
                    "observer_poll_seconds": 1,
                    "write_p95_seconds": percentile(write_times, 0.95) if write_times else None,
                    "transient_search_responses": len(transient_reads),
                    "scope": "simultaneous API writes through authorized reads and existing worker to both full-text and maintained relation",
                    "p95_seconds": percentile(elapsed, 0.95) if elapsed else None,
                    "max_seconds": round(max(elapsed), 3) if elapsed else None,
                    "errors": failures,
                }
            finally:
                stop_process(worker)
                worker_log.close()
            with app.state.db.transaction(owner["tenant_id"]) as session:
                report["concurrent_propagation"]["pending_index_requests"] = session.scalar(
                    text("SELECT count(*) FROM object_search_requests WHERE project_id=:project"),
                    {"project": owner["project_id"]},
                )
                report["concurrent_propagation"]["rule_work"] = [
                    {"status": status, "count": count}
                    for status, count in session.execute(
                        text(
                            "SELECT status,count(*) FROM relation_work WHERE project_id=:project GROUP BY status"
                        ),
                        {"project": owner["project_id"]},
                    )
                ]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report["measurements"], ensure_ascii=False), flush=True)
        if report["errors"] or report.get("concurrent_propagation", {}).get("errors"):
            raise RuntimeError(f"Benchmark failures recorded in {args.output}")
    finally:
        if server:
            stop_process(server)
        if app:
            app.state.db.engine.dispose()
        db.engine.dispose()
        with provisioner.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        provisioner.dispose()


if __name__ == "__main__":
    main()
