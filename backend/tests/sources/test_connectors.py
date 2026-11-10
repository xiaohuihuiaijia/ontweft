from __future__ import annotations

import sqlite3
from decimal import Decimal

import httpx
import pytest
from ontology.connectors import ConnectorError, NetworkPolicy, SQLConfig, SQLConnector, create_connector
from ontology.connectors.feishu import FeishuConnector, FeishuResource
from ontology.connectors.files import FileConnector
from ontology.connectors.http import HTTPConnector, HTTPResource, SafeHTTPClient
from ontology.connectors.neo4j import Neo4jConfig, Neo4jConnector
from ontology.connectors.rdf import RDFConnector
from pydantic import SecretStr


@pytest.fixture
def source_database(tmp_path):
    path = tmp_path / "source.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE account (tenant TEXT NOT NULL, id INTEGER NOT NULL, amount TEXT, payload BLOB, PRIMARY KEY (tenant,id))"
        )
        db.execute(
            "CREATE TABLE task (id INTEGER PRIMARY KEY, tenant TEXT, account_id INTEGER, FOREIGN KEY(tenant,account_id) REFERENCES account(tenant,id))"
        )
        db.execute("CREATE TABLE unkeyed (value TEXT)")
        db.executemany(
            "INSERT INTO account VALUES (?,?,?,?)",
            [("a", 1, "123456789.123456789", b"\x00\xff"), ("a", 2, None, b""), ("b", 1, "3", None)],
        )
    return path


def test_sql_discovery_and_transaction_snapshot_survive_concurrent_changes(source_database):
    connector = SQLConnector(SQLConfig(driver="sqlite", database=str(source_database)))
    try:
        resources = {r.resource_id: r for r in connector.discover()}
        assert resources["main.account"].primary_key == ["tenant", "id"]
        assert resources["main.task"].foreign_keys[0].fields == ["tenant", "account_id"]
        page = connector.read_snapshot("main.account", limit=1)
        assert page.records[0].values["payload"] == b"\x00\xff"
        assert page.records[0].values["amount"] == "123456789.123456789"
        assert not page.complete
        # A real concurrent commit must not change the already-open snapshot.
        with sqlite3.connect(source_database) as db:
            db.execute("DELETE FROM account WHERE tenant='a' AND id=2")
            db.execute("INSERT INTO account VALUES ('z',1,'9',NULL)")
        cursor = page.next_cursor
        page2 = connector.read_snapshot("main.account", cursor=cursor, limit=10)
        assert [r.key for r in page2.records] == [{"tenant": "a", "id": 2}, {"tenant": "b", "id": 1}]
        assert page2.complete and page2.snapshot_id == page.snapshot_id
        with pytest.raises(ConnectorError, match="expired"):
            connector.read_snapshot("main.account", cursor=cursor)
        fresh = connector.read_snapshot("main.account")
        assert fresh.records[-1].key == {"tenant": "z", "id": 1}
    finally:
        connector.close()


def test_sql_unknown_resource_unkeyed_and_closed_cursor_fail(source_database):
    connector = create_connector("sqlite", {"database": str(source_database)})
    assert connector.test_connection().ok
    with pytest.raises(ConnectorError) as exc:
        connector.read_snapshot("account; DROP TABLE account")
    assert exc.value.code == "unknown_resource"
    with pytest.raises(ConnectorError) as exc:
        connector.read_snapshot("main.unkeyed")
    assert exc.value.code == "missing_primary_key"
    assert not connector.sample("main.unkeyed").complete
    page = connector.read_snapshot("main.account", limit=1)
    connector.close()
    with pytest.raises(ConnectorError) as exc:
        connector.read_snapshot("main.account", cursor=page.next_cursor)
    assert exc.value.code == "invalid_cursor"
    assert not connector.capabilities.incremental
    with pytest.raises(ConnectorError) as exc:
        connector.read_changes()
    assert exc.value.code == "unsupported_capability"


def test_credentials_not_in_repr_errors_or_discovery():
    config = SQLConfig(
        driver="postgres",
        database="demo",
        host="not-allowed.invalid",
        username="reader",
        password="secret-do-not-print",
    )
    assert "secret-do-not-print" not in repr(config)
    connector = SQLConnector(config, network_policy=NetworkPolicy(frozenset()))
    check = connector.test_connection()
    assert check.code == "network_denied"
    assert "secret-do-not-print" not in check.model_dump_json()


def test_file_identity_and_escape_prevention(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    (root / "one.txt").write_text("文档正文", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    connector = FileConnector(root, {"stable-resource-id": "one.txt", "escape": "../secret.txt"})
    page = connector.read_snapshot("stable-resource-id")
    assert page.complete and page.records[0].key == {"resource_id": "stable-resource-id"}
    assert page.records[0].values["content"] == "文档正文".encode()
    with pytest.raises(ConnectorError) as exc:
        connector.read_snapshot("escape")
    assert exc.value.code == "path_denied"
    small = FileConnector(root, {"id": "one.txt"}, max_bytes=2)
    with pytest.raises(ConnectorError) as exc:
        small.read_snapshot("id")
    assert exc.value.code == "resource_limit"


def test_rdf_types_blank_scope_and_named_graph_reimport():
    data = b'@prefix x: <https://example.test/> . x:g { _:a x:name "hello"@en ; x:value "1.2300"^^<http://www.w3.org/2001/XMLSchema#decimal> . }'
    first = RDFConnector(data, format="trig")
    resources = first.discover()
    assert [r.resource_id for r in resources] == ["https://example.test/g"]
    page = first.read_snapshot(resources[0].resource_id, limit=1)
    rest = first.read_snapshot(resources[0].resource_id, cursor=page.next_cursor)
    rows = page.records + rest.records
    repeated = RDFConnector(data, format="trig").read_snapshot(resources[0].resource_id)
    assert rows == repeated.records
    assert any(r.values["object"].get("language") == "en" for r in rows)
    assert any(r.values["object"]["value"] == "1.2300" for r in rows)
    assert all(r.values["subject"]["kind"] == "blank" for r in rows)


def test_rdf_remote_context_and_xml_entities_rejected():
    remote = RDFConnector(b'{"@context":"https://example.test/context","@id":"urn:x"}', format="json-ld")
    assert remote.test_connection().code == "network_denied"
    xml = RDFConnector(
        b'<!DOCTYPE rdf [<!ENTITY external SYSTEM "file:///etc/passwd">]><rdf>&external;</rdf>', format="xml"
    )
    assert xml.test_connection().code == "invalid_rdf"


def http_client(handler, *, max_bytes=4096):
    return SafeHTTPClient(
        "https://localhost/open-apis/",
        network_policy=NetworkPolicy(frozenset({"localhost"}), allow_private_addresses=True),
        token=SecretStr("top-secret"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_bytes=max_bytes,
    )


def test_http_pagination_preserves_decimal_and_detects_token_loop():
    calls = []

    def handler(request):
        calls.append(request)
        if "cursor" not in request.url.params:
            return httpx.Response(
                200, content=b'{"items":[{"id":1,"v":1.1234567890123456789}],"next_cursor":"a"}'
            )
        return httpx.Response(200, json={"items": [{"id": 2}], "next_cursor": "a"})

    adapter = HTTPConnector(
        http_client(handler), [HTTPResource(resource_id="records", path="records", key_fields=["id"])]
    )
    first = adapter.read_snapshot("records", limit=1)
    assert first.records[0].values["v"] == Decimal("1.1234567890123456789")
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("records", cursor=first.next_cursor, limit=1)
    assert exc.value.code == "pagination_loop"
    assert calls[0].headers["authorization"] == "Bearer top-secret"
    adapter.close()


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(302, headers={"Location": "https://evil.test"}), "http_error"),
        (httpx.Response(429), "rate_limited"),
        (httpx.Response(403), "permission_denied"),
        (httpx.Response(200, content=b"12345"), "resource_limit"),
    ],
)
def test_http_redirects_limits_permissions_and_errors_are_explicit(response, code):
    client = http_client(lambda request: response, max_bytes=4)
    with pytest.raises(ConnectorError) as exc:
        client.get_bytes("records")
    assert exc.value.code == code
    assert "top-secret" not in str(exc.value)
    client.close()


def test_http_cross_origin_and_network_deny_before_request():
    def fail(request):
        pytest.fail("must not send a denied request")

    client = http_client(fail)
    with pytest.raises(ConnectorError) as exc:
        client.get_bytes("https://elsewhere.test/data")
    assert exc.value.code == "network_denied"
    with pytest.raises(ConnectorError):
        NetworkPolicy(frozenset({"localhost"})).check_url("https://localhost/private")
    client.close()


def test_feishu_blocks_are_not_flattened_and_app_read_is_not_employee_authorization():
    def handler(request):
        assert request.url.path == "/open-apis/docx/v1/documents/doc123/blocks"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [
                        {
                            "block_id": "b1",
                            "block_type": 2,
                            "text": {"elements": [{"text_run": {"content": "中文正文"}}]},
                        }
                    ],
                    "has_more": False,
                },
            },
        )

    adapter = FeishuConnector(
        http_client(handler),
        [FeishuResource(resource_id="doc", kind="docx", resource_token="doc123")],
        credential_kind="application",
    )
    page = adapter.read_snapshot("doc")
    assert page.records[0].values["text"]["elements"][0]["text_run"]["content"] == "中文正文"
    assert adapter.source_permission().decision == "unknown"
    assert not adapter.capabilities.source_permissions
    adapter.close()


def test_feishu_missing_continuation_does_not_mark_scan_complete():
    client = http_client(
        lambda request: httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": True}})
    )
    adapter = FeishuConnector(
        client, [FeishuResource(resource_id="doc", kind="docx", resource_token="doc123")]
    )
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("doc")
    assert exc.value.code == "pagination_loop"
    adapter.close()


def test_http_failed_page_can_retry_same_cursor_without_skipping_records():
    attempts = 0

    def handler(request):
        nonlocal attempts
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"items": [{"id": 1}], "next_cursor": "second"})
        attempts += 1
        if attempts == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"items": [{"id": 2}], "next_cursor": None})

    adapter = HTTPConnector(
        http_client(handler), [HTTPResource(resource_id="records", path="records", key_fields=["id"])]
    )
    first = adapter.read_snapshot("records", limit=1)
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("records", cursor=first.next_cursor, limit=1)
    assert exc.value.code == "rate_limited"
    last = adapter.read_snapshot("records", cursor=first.next_cursor, limit=1)
    assert last.complete and last.records[0].key == {"id": 2}
    assert last.snapshot_id == first.snapshot_id
    adapter.close()


def test_http_missing_completion_field_cannot_confirm_inventory_completion():
    adapter = HTTPConnector(
        http_client(lambda request: httpx.Response(200, json={"items": []})),
        [HTTPResource(resource_id="records", path="records", key_fields=["id"])],
    )
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("records")
    assert exc.value.code == "incomplete_response"
    adapter.close()


def graph_connector():
    return Neo4jConnector(
        Neo4jConfig(
            uri="bolt://localhost:7687",
            username="reader",
            password="redacted-password",
            label_keys={"Person": "employee_id", "Project": "project_id"},
            relationships={
                "memberships": {
                    "relationship_type": "ASSIGNED`TO",
                    "source_label": "Person",
                    "target_label": "Project",
                    "key_fields": ["tenant", "assignment_id"],
                }
            },
        ),
        network_policy=NetworkPolicy(frozenset({"localhost"}), allow_private_addresses=True),
    )


def graph_preflight(query):
    if "count(*) AS c" in query:
        return []
    if "valueType" in query:
        return (
            [{"k0": "STRING NOT NULL", "k1": "INTEGER NOT NULL"}]
            if "valueType(r." in query
            else [{"k0": "STRING NOT NULL"}]
        )
    return None


def test_neo4j_relationship_protocol_preserves_parallel_edges_endpoints_and_composite_cursor(monkeypatch):
    # Scripted driver boundary, not a claim of running a Neo4j service.
    adapter, queries = graph_connector(), []
    rows = [
        {
            "properties": {"tenant": "a' RETURN secret", "assignment_id": number},
            "source_key": "employee-1",
            "target_key": "project-1",
        }
        for number in (1, 2)
    ]

    def query(text, **params):
        queries.append((text, params))
        if "db.labels()" in text:
            return [{"label": "Person"}, {"label": "Project"}]
        if "db.relationshipTypes()" in text:
            return [{"relationshipType": "ASSIGNED`TO"}]
        preflight = graph_preflight(text)
        if preflight is not None:
            return preflight
        assert "[r:`ASSIGNED``TO`]->" in text
        assert "a' RETURN secret" not in text
        assert "elementId(" not in text and "id(" not in text
        return rows[1:] if "key0" in params else rows

    monkeypatch.setattr(adapter, "_query", query)
    discovered = {r.resource_id: r for r in adapter.discover()}
    relationship = discovered["memberships"]
    assert relationship.kind == "graph_relationships"
    assert relationship.primary_key == ["tenant", "assignment_id"]
    assert relationship.foreign_keys[0].target_resource == "Person"
    first = adapter.read_snapshot("memberships", limit=1)
    last = adapter.read_snapshot("memberships", cursor=first.next_cursor, limit=1)
    assert first.records[0].key != last.records[0].key
    assert first.records[0].values["source"] == {
        "resource_id": "Person",
        "key": {"employee_id": "employee-1"},
    }
    assert first.records[0].values["target"] == {"resource_id": "Project", "key": {"project_id": "project-1"}}
    assert first.snapshot_id == last.snapshot_id and last.complete
    assert not adapter.capabilities.consistent_snapshot and not adapter.capabilities.deletion_reconciliation
    statement, parameters = queries[-1]
    assert "(r.`tenant`>$key0) OR (r.`tenant`=$key0 AND r.`assignment_id`>$key1)" in statement
    assert parameters == {"limit": 2, "key0": "a' RETURN secret", "key1": 1}
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("memberships", cursor=first.next_cursor)
    assert exc.value.code == "invalid_cursor"


@pytest.mark.parametrize(
    "failure,code", [("duplicate", "invalid_primary_key"), ("mixed", "unsupported_key_type")]
)
def test_neo4j_bad_endpoint_keys_reject_relationship_enumeration(monkeypatch, failure, code):
    adapter = graph_connector()

    def query(text, **params):
        assert "properties(r)" not in text
        if "count(*)" in text:
            return [{"c": 2}] if failure == "duplicate" else []
        return [{"k0": "STRING NOT NULL"}, {"k0": "INTEGER NOT NULL"}]

    monkeypatch.setattr(adapter, "_query", query)
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("memberships")
    assert exc.value.code == code


def test_neo4j_cursor_failure_is_retryable_and_config_scope_is_bound(monkeypatch):
    adapter, attempts = graph_connector(), 0

    def query(text, **params):
        nonlocal attempts
        preflight = graph_preflight(text)
        if preflight is not None:
            return preflight
        if "key0" not in params:
            return [{"properties": {"employee_id": "a"}}, {"properties": {"employee_id": "b"}}]
        attempts += 1
        if attempts == 1:
            raise ConnectorError("source_read_failed", "Transient source failure", retryable=True)
        return [{"properties": {"employee_id": "b"}}]

    monkeypatch.setattr(adapter, "_query", query)
    first = adapter.read_snapshot("Person", limit=1)
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("Project", cursor=first.next_cursor)
    assert exc.value.code == "invalid_cursor"
    with pytest.raises(ConnectorError):
        adapter.read_snapshot("Person", cursor=first.next_cursor)
    last = adapter.read_snapshot("Person", cursor=first.next_cursor)
    assert last.complete and last.records[0].key == {"employee_id": "b"}
    another = adapter.read_snapshot("Person", limit=1)
    adapter.config.label_keys["Person"] = "renamed_key"
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("Person", cursor=another.next_cursor)
    assert exc.value.code == "invalid_cursor"


def test_neo4j_typed_properties_keep_nanosecond_duration_point_and_bytes(monkeypatch):
    from neo4j.spatial import WGS84Point
    from neo4j.time import DateTime, Duration

    adapter = graph_connector()
    when = DateTime(2026, 10, 28, 12, 0, 0, nanosecond=123456789)

    def query(text, **params):
        preflight = graph_preflight(text)
        if preflight is not None:
            return preflight
        return [
            {
                "properties": {
                    "employee_id": "a",
                    "when": when,
                    "duration": Duration(months=1, nanoseconds=1),
                    "where": WGS84Point((120.5, 31.2)),
                    "bytes": bytearray(b"\xff"),
                    "float": float("inf"),
                }
            }
        ]

    monkeypatch.setattr(adapter, "_query", query)
    page = adapter.read_snapshot("Person")
    values = page.records[0].values
    assert values["when"]["value"].endswith(".123456789")
    assert values["duration"]["value"] == "P1MT0.000000001S"
    assert values["where"]["srid"] == 4326
    assert values["bytes"] == b"\xff" and values["float"]["value"] == "inf"
    assert "redacted-password" not in page.model_dump_json()


def test_neo4j_factory_rejects_relationship_without_stable_endpoint_mapping():
    with pytest.raises(ConnectorError) as exc:
        create_connector(
            "neo4j",
            {
                "uri": "bolt://localhost",
                "username": "reader",
                "password": "hidden",
                "relationships": {
                    "edge": {
                        "relationship_type": "REL",
                        "source_label": "A",
                        "target_label": "B",
                        "key_fields": ["id"],
                    }
                },
            },
            network_policy=NetworkPolicy(frozenset()),
        )
    assert exc.value.code == "invalid_config" and "hidden" not in str(exc.value)


def test_decimal_identity_is_scale_invariant_and_independent_of_context_precision():
    from decimal import localcontext

    from ontology.connectors.base import canonical_identity_key

    with localcontext() as context:
        context.prec = 2
        assert canonical_identity_key({"id": Decimal("1.0")}) == canonical_identity_key(
            {"id": Decimal("1.00")}
        )
        assert canonical_identity_key({"id": Decimal("-0.000")}) == canonical_identity_key({"id": Decimal(0)})
        big = canonical_identity_key({"id": Decimal("123456789012345678901234567890.12345000")})
        assert big["id"] == {"type": "decimal", "value": "12345678901234567890123456789012345e-5"}
        assert canonical_identity_key({"id": 1}) != canonical_identity_key({"id": "1"})


def test_sqlite_nocase_primary_key_is_not_treated_as_binary_identity(tmp_path):
    path = tmp_path / "folded.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE folded (id TEXT COLLATE NOCASE PRIMARY KEY)")
        db.execute("INSERT INTO folded VALUES ('Alice')")
    adapter = SQLConnector(SQLConfig(driver="sqlite", database=str(path)))
    try:
        with pytest.raises(ConnectorError) as exc:
            adapter.read_snapshot("main.folded")
        assert exc.value.code == "unsupported_key_semantics"
        assert adapter.sample("main.folded").records[0].key == {"id": "Alice"}
    finally:
        adapter.close()
