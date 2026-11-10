"""Opt-in real databases; create and remove only labelled task-owned containers.

PowerShell: $env:ONTOLOGY_LIVE_CONNECTORS='postgres,mysql'; python -m pytest
backend/tests/live/test_live_connectors.py -q. Images are pinned, services run serially,
credentials stay in ignored var/live-connectors/<run>/server.env.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
import uuid
from decimal import Decimal

import pytest
from ontology.connectors import NetworkPolicy, SQLConfig, SQLConnector
from ontology.connectors.mongo import MongoConfig, MongoConnector
from ontology.connectors.neo4j import Neo4jConfig, Neo4jConnector
from pydantic import SecretStr

from tests.support.paths import REPO_ROOT

IMAGES = {
    "postgres": "postgres@sha256:7ad98329d513dd497293b951c195ca354274a77f12ddbbbbf85e68a811823d72",
    "mysql": "mysql@sha256:7dcddc01f13bab2f15cde676d44d01f61fc9f99fe7785e86196dfc07d358ae2b",
    "neo4j": "neo4j@sha256:414adfe41c6ff32abb752be37f265c77607c166cab298f65a917fc4f05776218",
    "mongo": "mongo@sha256:b9924c330a8bbc5800fe9c69f04a84e221eca7b28fd049a0354147d9908fa142",
}

REQUESTED = set(os.environ.get("ONTOLOGY_LIVE_CONNECTORS", "").split(","))

if REQUESTED & {"neo4j", "mongo"} and len(REQUESTED & set(IMAGES)) > 1:
    raise pytest.UsageError(
        "Run SQL, Neo4j and MongoDB in separate pytest invocations so only one test service is active"
    )

POLICY = NetworkPolicy(frozenset({"127.0.0.1"}), allow_private_addresses=True)


def docker(*args):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8", timeout=60, check=False
    )
    if result.returncode:
        raise RuntimeError("Task-owned Docker operation failed")
    return result.stdout.strip()


def admin_connection(engine, port, password):
    if engine == "postgres":
        import pg8000.dbapi

        conn = pg8000.dbapi.connect(
            host="127.0.0.1",
            port=port,
            user="postgres",
            password=password,
            database="ontology_test",
            timeout=5,
        )
        conn.autocommit = True
        return conn
    import pymysql

    return pymysql.connect(
        host="127.0.0.1",
        port=port,
        user="root",
        password=password,
        database="ontology_test",
        connect_timeout=5,
        autocommit=True,
    )


def execute(conn, sql, args=()):
    __tracebackhide__ = True
    cur = conn.cursor()
    try:
        if args:
            cur.execute(sql, args)
        else:
            cur.execute(sql)
        return cur.fetchall() if cur.description else []
    except Exception:  # noqa: BLE001 -- test setup SQL can contain one-time secrets; redact the driver traceback.
        raise RuntimeError("Test database SQL operation failed (driver details redacted)") from None
    finally:
        cur.close()


@pytest.fixture(scope="module", params=["postgres", "mysql"])
def live_database(request):
    engine = request.param
    if engine not in REQUESTED:
        pytest.skip("Explicit ONTOLOGY_LIVE_CONNECTORS opt-in required")
    run_id = uuid.uuid4().hex[:12]
    name = f"ontology-source-test-{engine}-{run_id}"
    directory = REPO_ROOT / "var" / "live-connectors" / run_id
    directory.mkdir(parents=True)
    admin_password, reader_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    envfile = directory / "server.env"
    envfile.write_text(
        (
            f"POSTGRES_PASSWORD={admin_password}\nPOSTGRES_DB=ontology_test\n"
            if engine == "postgres"
            else f"MYSQL_ROOT_PASSWORD={admin_password}\nMYSQL_DATABASE=ontology_test\n"
        )
        + f"ONTOLOGY_TEST_READER_PASSWORD={reader_password}\n",
        encoding="utf-8",
    )
    docker("image", "inspect", IMAGES[engine], "--format", "{{.Id}}")
    label = "ontology.connector-test-run"
    internal_port = 5432 if engine == "postgres" else 3306
    created, conn = False, None
    try:
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            f"{label}={run_id}",
            "--memory",
            "768m" if engine == "postgres" else "1g",
            "--cpus",
            "1",
            "--env-file",
            str(envfile),
            "--publish",
            f"127.0.0.1::{internal_port}",
            IMAGES[engine],
        )
        created = True
        binding = docker("port", name, f"{internal_port}/tcp")
        if not binding.startswith("127.0.0.1:"):
            raise RuntimeError("Live-test database was not bound exclusively to loopback")
        port = int(binding.rsplit(":", 1)[1])
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                conn = admin_connection(engine, port, admin_password)
                break
            except Exception:  # noqa: BLE001 -- startup retries never print credentials or driver errors.
                time.sleep(1)
        if conn is None:
            raise RuntimeError("Task-owned database did not become ready")
        if engine == "postgres":
            execute(conn, f"CREATE ROLE ontology_reader LOGIN PASSWORD '{reader_password}'")
        else:
            execute(conn, f"CREATE USER 'ontology_reader'@'%' IDENTIFIED BY '{reader_password}'")
        schema = "public" if engine == "postgres" else "ontology_test"
        version = execute(conn, "SELECT version()")[0][0]
        (directory / "result-context.json").write_text(
            json.dumps(
                {
                    "engine": engine,
                    "image": IMAGES[engine],
                    "server_version": version,
                    "container": name,
                    "port": port,
                    "scope": "task-owned disposable database",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        yield {
            "engine": engine,
            "port": port,
            "schema": schema,
            "admin": conn,
            "password": SecretStr(reader_password),
        }
    finally:
        if conn is not None:
            conn.close()
        if created:
            labels = json.loads(docker("inspect", name, "--format", "{{json .Config.Labels}}"))
            if labels.get(label) != run_id or not name.startswith("ontology-source-test-"):
                raise RuntimeError("Refusing to clean a container not owned by this test run")
            docker("rm", "--force", name)


@pytest.fixture
def live_sql(live_database):
    state = live_database
    conn, engine = state["admin"], state["engine"]
    quote = '"' if engine == "postgres" else "`"
    for table in ("task", "account", "unkeyed", "odd table"):
        execute(conn, f"DROP TABLE IF EXISTS {quote}{table}{quote}")
    binary = "BYTEA" if engine == "postgres" else "BLOB"
    text_type = (
        "VARCHAR(40)"
        if engine == "postgres"
        else "VARCHAR(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
    )
    execute(
        conn,
        f"CREATE TABLE account (tenant {text_type} NOT NULL,id INTEGER NOT NULL,amount DECIMAL(30,18),payload {binary},PRIMARY KEY(tenant,id))",
    )
    execute(
        conn,
        f"CREATE TABLE task (id INTEGER PRIMARY KEY,tenant {text_type},account_id INTEGER,FOREIGN KEY(tenant,account_id) REFERENCES account(tenant,id))",
    )
    execute(conn, "CREATE TABLE unkeyed (value VARCHAR(40))")
    execute(conn, f"CREATE TABLE {quote}odd table{quote} (id INTEGER PRIMARY KEY)")
    for row in [
        ("a", 1, Decimal("123456789.123456789123456789"), b"\x00\xff"),
        ("a", 2, Decimal(2), None),
        ("b", 1, Decimal(3), None),
    ]:
        execute(conn, "INSERT INTO account (tenant,id,amount,payload) VALUES (%s,%s,%s,%s)", row)
    execute(conn, "INSERT INTO unkeyed VALUES ('sample')")
    if engine == "postgres":
        execute(conn, "GRANT USAGE ON SCHEMA public TO ontology_reader")
        execute(conn, "GRANT SELECT ON ALL TABLES IN SCHEMA public TO ontology_reader")
    else:
        execute(conn, "GRANT SELECT ON ontology_test.* TO 'ontology_reader'@'%'")
    config = SQLConfig(
        driver=engine,
        database="ontology_test",
        host="127.0.0.1",
        port=state["port"],
        username="ontology_reader",
        password=state["password"],
        timeout_seconds=5,
    )
    adapter = SQLConnector(config, network_policy=POLICY)
    try:
        yield adapter, state
    finally:
        adapter.close()


def graph_command(driver, statement, **parameters):
    __tracebackhide__ = True
    try:
        with driver.session(database="neo4j") as session:
            return session.run(statement, parameters).data()
    except Exception:  # noqa: BLE001 -- test fixture errors cannot expose connection secrets.
        raise RuntimeError("Test graph operation failed (driver details redacted)") from None


@pytest.fixture(scope="module")
def live_graph_service():
    if "neo4j" not in REQUESTED:
        pytest.skip("Explicit Neo4j live-test opt-in required")
    from neo4j import GraphDatabase

    run_id = uuid.uuid4().hex[:12]
    name = f"ontology-source-test-neo4j-{run_id}"
    label = "ontology.connector-test-run"
    directory = REPO_ROOT / "var" / "live-connectors" / run_id
    directory.mkdir(parents=True)
    password = secrets.token_urlsafe(32)
    envfile = directory / "server.env"
    envfile.write_text(
        f"NEO4J_AUTH=neo4j/{password}\nNEO4J_server_memory_heap_initial__size=256m\nNEO4J_server_memory_heap_max__size=256m\nNEO4J_server_memory_pagecache_size=128m\n",
        encoding="utf-8",
    )
    created, driver = False, None
    try:
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            f"{label}={run_id}",
            "--memory",
            "1g",
            "--cpus",
            "1",
            "--env-file",
            str(envfile),
            "--publish",
            "127.0.0.1::7687",
            IMAGES["neo4j"],
        )
        created = True
        binding = docker("port", name, "7687/tcp")
        if not binding.startswith("127.0.0.1:"):
            raise RuntimeError("Graph test service is not bound to loopback")
        uri = "bolt://" + binding
        driver = GraphDatabase.driver(
            uri,
            auth=("neo4j", password),
            connection_timeout=3,
            connection_acquisition_timeout=5,
            max_transaction_retry_time=0,
        )
        deadline = time.monotonic() + 120
        ready = False
        while time.monotonic() < deadline:
            try:
                driver.verify_connectivity()
                ready = True
                break
            except Exception:  # noqa: BLE001 -- bounded startup retries, no driver messages.
                time.sleep(1)
        if not ready:
            raise RuntimeError("Task-owned graph did not become ready")
        version = graph_command(driver, "CALL dbms.components() YIELD versions RETURN versions")[0][
            "versions"
        ]
        (directory / "result-context.json").write_text(
            json.dumps(
                {
                    "engine": "neo4j",
                    "image": IMAGES["neo4j"],
                    "server_version": version,
                    "container": name,
                    "scope": "task-owned disposable graph; admin fixture account, generated read-only adapter queries",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        yield (
            driver,
            Neo4jConfig(
                uri=uri,
                username="neo4j",
                password=SecretStr(password),
                label_keys={"Person": "employee_id", "Project": "project_id"},
                relationships={
                    "assignments": {
                        "relationship_type": "ASSIGNED`TO",
                        "source_label": "Person",
                        "target_label": "Project",
                        "key_fields": ["tenant", "assignment_id"],
                    }
                },
                timeout_seconds=3,
            ),
        )
    finally:
        if driver is not None:
            driver.close()
        if created:
            labels = json.loads(docker("inspect", name, "--format", "{{json .Config.Labels}}"))
            if labels.get(label) != run_id or not name.startswith("ontology-source-test-"):
                raise RuntimeError("Refusing graph cleanup outside this test run")
            docker("rm", "--force", name)


@pytest.fixture
def live_graph(live_graph_service):
    driver, config = live_graph_service
    # This driver is created solely for the labelled disposable fixture above.
    graph_command(driver, "MATCH (n) DETACH DELETE n")
    graph_command(
        driver,
        "CREATE (a:Person {employee_id:'e1',when:localdatetime('2026-10-28T12:00:00.123456789'),where:point({longitude:120.5,latitude:31.2})}), (b:Person {employee_id:'e2'}), (p:Project {project_id:'p1'}), (q:Project {project_id:'p2'}), (a)-[:`ASSIGNED``TO` {tenant:'a',assignment_id:1}]->(p), (a)-[:`ASSIGNED``TO` {tenant:'a',assignment_id:2}]->(p), (b)-[:`ASSIGNED``TO` {tenant:'a',assignment_id:3}]->(q)",
    )
    adapter = Neo4jConnector(config.model_copy(deep=True), network_policy=POLICY)
    try:
        yield adapter, driver
    finally:
        adapter.close()


@pytest.fixture(scope="module")
def live_mongo_service():
    if "mongo" not in REQUESTED:
        pytest.skip("Explicit MongoDB live-test opt-in required")
    from pymongo import MongoClient

    run_id = uuid.uuid4().hex[:12]
    name, label = f"ontology-source-test-mongo-{run_id}", "ontology.connector-test-run"
    directory = REPO_ROOT / "var" / "live-connectors" / run_id
    directory.mkdir(parents=True)
    password, reader_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    envfile = directory / "server.env"
    envfile.write_text(
        f"MONGO_INITDB_ROOT_USERNAME=ontology_admin\nMONGO_INITDB_ROOT_PASSWORD={password}\nONTOLOGY_TEST_READER_PASSWORD={reader_password}\n",
        encoding="utf-8",
    )
    client, created = None, False
    try:
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            f"{label}={run_id}",
            "--memory",
            "768m",
            "--cpus",
            "1",
            "--env-file",
            str(envfile),
            "--publish",
            "127.0.0.1::27017",
            IMAGES["mongo"],
        )
        created = True
        binding = docker("port", name, "27017/tcp")
        if not binding.startswith("127.0.0.1:"):
            raise RuntimeError("MongoDB test service is not bound to loopback")
        port = int(binding.rsplit(":", 1)[1])
        client = MongoClient(
            "127.0.0.1",
            port,
            username="ontology_admin",
            password=password,
            authSource="admin",
            directConnection=True,
            serverSelectionTimeoutMS=2000,
            connectTimeoutMS=2000,
            socketTimeoutMS=2000,
        )
        deadline = time.monotonic() + 90
        ready = False
        while time.monotonic() < deadline:
            try:
                client.admin.command("ping")
                ready = True
                break
            except Exception:  # noqa: BLE001 -- do not expose startup credentials.
                time.sleep(1)
        if not ready:
            raise RuntimeError("Task-owned MongoDB did not become ready")
        try:
            client.ontology_test.command(
                "createUser",
                "ontology_reader",
                pwd=reader_password,
                roles=[{"role": "read", "db": "ontology_test"}],
            )
        except Exception:  # noqa: BLE001 -- createUser commands contain a one-time secret.
            raise RuntimeError("Test MongoDB user creation failed (details redacted)") from None
        (directory / "result-context.json").write_text(
            json.dumps(
                {
                    "engine": "mongo",
                    "image": IMAGES["mongo"],
                    "server_version": client.server_info()["version"],
                    "container": name,
                    "scope": "task-owned disposable MongoDB with read-only source user",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        yield (
            client,
            MongoConfig(
                host="127.0.0.1",
                port=port,
                database="ontology_test",
                username="ontology_reader",
                password=SecretStr(reader_password),
                auth_source="ontology_test",
                tls=False,
                timeout_seconds=3,
                collections=["records", "mixed"],
            ),
        )
    finally:
        if client is not None:
            client.close()
        if created:
            labels = json.loads(docker("inspect", name, "--format", "{{json .Config.Labels}}"))
            if labels.get(label) != run_id or not name.startswith("ontology-source-test-"):
                raise RuntimeError("Refusing MongoDB cleanup outside this test run")
            docker("rm", "--force", name)


@pytest.fixture
def live_mongo(live_mongo_service):
    from bson import Binary, Decimal128, Int64, ObjectId

    client, config = live_mongo_service
    db = client.ontology_test
    db.records.drop()
    db.mixed.drop()
    db.records.insert_many(
        [
            {
                "_id": ObjectId("000000000000000000000001"),
                "amount": Decimal128("123456789.123456789123456789"),
                "payload": Binary(b"\x00\xff"),
                "long": Int64(9223372036854775807),
                "nil": None,
                "nested": {"list": [1, "a"]},
            },
            {"_id": ObjectId("000000000000000000000002"), "amount": Decimal128("2.00")},
        ]
    )
    db.mixed.insert_many([{"_id": 1}, {"_id": "a"}, {"_id": ObjectId("000000000000000000000003")}])
    adapter = MongoConnector(config.model_copy(deep=True), network_policy=POLICY)
    try:
        yield adapter, db
    finally:
        adapter.close()
