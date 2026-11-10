"""Opt-in real databases; create and remove only labelled task-owned containers.

PowerShell: $env:ONTOLOGY_LIVE_CONNECTORS='postgres,mysql'; python -m pytest
backend/tests/live/test_live_connectors.py -q. Images are pinned, services run serially,
credentials stay in ignored var/live-connectors/<run>/server.env.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from ontology.connectors import ConnectorError

from tests.support.live_connectors import (
    execute,
    graph_command,
)
from tests.support.live_connectors import live_database as live_database  # noqa: PLC0414 - pytest fixture registration
from tests.support.live_connectors import live_graph as live_graph  # noqa: PLC0414 - pytest fixture registration
from tests.support.live_connectors import live_graph_service as live_graph_service  # noqa: PLC0414 - pytest fixture registration
from tests.support.live_connectors import live_mongo as live_mongo  # noqa: PLC0414 - pytest fixture registration
from tests.support.live_connectors import live_mongo_service as live_mongo_service  # noqa: PLC0414 - pytest fixture registration
from tests.support.live_connectors import live_sql as live_sql  # noqa: PLC0414 - pytest fixture registration


def test_live_discover_composite_foreign_keys_precision_and_quoted_tables(live_sql):
    adapter, state = live_sql
    assert adapter.test_connection().ok
    resources = {r.resource_id: r for r in adapter.discover()}
    prefix = state["schema"] + "."
    assert resources[prefix + "account"].primary_key == ["tenant", "id"]
    amount = next(field for field in resources[prefix + "account"].fields if field.name == "amount")
    assert amount.metadata["numeric_precision"] == 30 and amount.metadata["numeric_scale"] == 18
    fk = resources[prefix + "task"].foreign_keys[0]
    assert fk.fields == ["tenant", "account_id"] and fk.target_fields == ["tenant", "id"]
    assert fk.target_resource == prefix + "account"
    assert adapter.read_snapshot(prefix + "odd table").complete
    first = adapter.read_snapshot(prefix + "account", limit=1)
    assert first.records[0].values["amount"] == Decimal("123456789.123456789123456789")
    assert first.records[0].values["payload"] == b"\x00\xff"
    assert first.consistency == "transaction"


def test_live_snapshot_survives_concurrent_commit_and_new_scan_sees_changes(live_sql):
    adapter, state = live_sql
    resource = state["schema"] + ".account"
    first = adapter.read_snapshot(resource, limit=1)
    execute(state["admin"], "DELETE FROM account WHERE tenant='a' AND id=2")
    execute(state["admin"], "UPDATE account SET amount=99 WHERE tenant='b'")
    execute(state["admin"], "INSERT INTO account (tenant,id,amount) VALUES ('z',1,9)")
    final = adapter.read_snapshot(resource, cursor=first.next_cursor, limit=10)
    assert final.snapshot_id == first.snapshot_id and final.complete
    assert [r.key for r in final.records] == [{"tenant": "a", "id": 2}, {"tenant": "b", "id": 1}]
    assert final.records[-1].values["amount"] == Decimal(3)
    fresh = adapter.read_snapshot(resource)
    assert fresh.records[-1].key == {"tenant": "z", "id": 1}
    assert fresh.records[1].values["amount"] == Decimal(99)


def test_live_source_account_readonly_no_key_and_out_of_scope_are_rejected(live_sql):
    adapter, state = live_sql
    prefix = state["schema"] + "."
    with pytest.raises(ConnectorError):
        adapter._rows("INSERT INTO unkeyed VALUES ('not-authorized')")
    assert execute(state["admin"], "SELECT count(*) FROM unkeyed")[0][0] == 1
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot(prefix + "unkeyed")
    assert exc.value.code == "missing_primary_key"
    assert adapter.sample(prefix + "unkeyed").records[0].values["value"] == "sample"
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("outside.account")
    assert exc.value.code == "unknown_resource"


def test_live_terminated_source_snapshot_requires_restart(live_sql):
    adapter, state = live_sql
    query = (
        "SELECT pg_backend_pid() AS id" if state["engine"] == "postgres" else "SELECT CONNECTION_ID() AS id"
    )
    connection_id = int(adapter._rows(query)[0]["id"])
    resource = state["schema"] + ".account"
    first = adapter.read_snapshot(resource, limit=1)
    if state["engine"] == "postgres":
        execute(state["admin"], "SELECT pg_terminate_backend(%s)", (connection_id,))
    else:
        execute(state["admin"], f"KILL CONNECTION {connection_id}")
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot(resource, cursor=first.next_cursor, limit=10)
    assert exc.value.code == "snapshot_lost"
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot(resource, cursor=first.next_cursor)
    assert exc.value.code == "invalid_cursor"
    assert adapter.read_snapshot(resource).complete


def test_live_column_drift_fails_closed_until_rediscovery(live_sql):
    adapter, state = live_sql
    resource = state["schema"] + ".account"
    adapter.discover()
    execute(state["admin"], "ALTER TABLE account ADD COLUMN added_field VARCHAR(40)")
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot(resource)
    assert exc.value.code == "schema_changed"
    resources = {r.resource_id: r for r in adapter.discover()}
    assert resources[resource].fields[-1].name == "added_field"
    assert adapter.read_snapshot(resource).records[0].values["added_field"] is None


def test_live_first_page_does_not_buffer_entire_table(live_sql):
    adapter, state = live_sql
    cur = state["admin"].cursor()
    try:
        cur.executemany(
            "INSERT INTO account (tenant,id,amount) VALUES (%s,%s,%s)",
            [("stream", i, i) for i in range(2000)],
        )
    finally:
        cur.close()
    first = adapter.read_snapshot(state["schema"] + ".account", limit=5)
    assert len(first.records) == 5 and first.next_cursor
    page_state = adapter._pages[first.next_cursor]
    assert len(page_state["buffer"]) == 1
    if state["engine"] == "postgres":
        assert page_state["server_cursor"]
        assert len(page_state["cursor"]._context.rows) <= 6
    else:
        from pymysql.cursors import SSCursor

        assert isinstance(page_state["cursor"], SSCursor)
        assert page_state["cursor"]._result.unbuffered_active


def test_live_mysql_nontransactional_table_does_not_claim_consistent_snapshot(live_sql):
    adapter, state = live_sql
    if state["engine"] != "mysql":
        pytest.skip("MySQL storage engine boundary")
    execute(state["admin"], "DROP TABLE IF EXISTS legacy_engine")
    execute(state["admin"], "CREATE TABLE legacy_engine (id INTEGER PRIMARY KEY) ENGINE=MyISAM")
    try:
        with pytest.raises(ConnectorError) as exc:
            adapter.read_snapshot("ontology_test.legacy_engine")
        assert exc.value.code == "unsupported_snapshot"
        assert not adapter.sample("ontology_test.legacy_engine").complete
    finally:
        execute(state["admin"], "DROP TABLE legacy_engine")


def test_live_numeric_primary_key_scale_update_retains_identity(live_sql):
    adapter, state = live_sql
    if state["engine"] != "postgres":
        pytest.skip("PostgreSQL unconstrained NUMERIC preserves stored scale")
    conn = state["admin"]
    execute(conn, "CREATE TABLE numeric_identity (id NUMERIC PRIMARY KEY)")
    try:
        execute(conn, "GRANT SELECT ON numeric_identity TO ontology_reader")
        execute(conn, "INSERT INTO numeric_identity VALUES (1.0)")
        first = adapter.read_snapshot("public.numeric_identity").records[0]
        execute(conn, "UPDATE numeric_identity SET id=1.00")
        second = adapter.read_snapshot("public.numeric_identity").records[0]
        assert str(first.key["id"]) == "1.0" and str(second.key["id"]) == "1.00"
        assert first.identity_key == second.identity_key == {"id": {"type": "decimal", "value": "1"}}
    finally:
        execute(conn, "DROP TABLE numeric_identity")


def test_live_case_insensitive_primary_key_requires_explicit_canonical_identity(live_sql):
    adapter, state = live_sql
    conn = state["admin"]
    if state["engine"] == "postgres":
        execute(conn, "CREATE EXTENSION IF NOT EXISTS citext")
        key_type = "citext"
    else:
        key_type = "VARCHAR(40) COLLATE utf8mb4_0900_ai_ci"
    execute(conn, f"CREATE TABLE folded_identity (id {key_type} PRIMARY KEY)")
    try:
        if state["engine"] == "postgres":
            execute(conn, "GRANT SELECT ON folded_identity TO ontology_reader")
        execute(conn, "INSERT INTO folded_identity VALUES ('Alice')")
        with pytest.raises(ConnectorError) as exc:
            adapter.read_snapshot(state["schema"] + ".folded_identity")
        assert exc.value.code == "unsupported_key_semantics"
        assert adapter.sample(state["schema"] + ".folded_identity").records[0].key == {"id": "Alice"}
    finally:
        execute(conn, "DROP TABLE folded_identity")


def test_live_neo4j_node_types_directed_parallel_edges_and_composite_pagination(live_graph):
    adapter, _ = live_graph
    assert adapter.test_connection().ok
    resources = {item.resource_id: item for item in adapter.discover()}
    assert resources["assignments"].kind == "graph_relationships"
    assert resources["assignments"].primary_key == ["tenant", "assignment_id"]
    nodes = adapter.read_snapshot("Person")
    assert nodes.records[0].values["when"]["value"].endswith(".123456789")
    assert nodes.records[0].values["where"]["srid"] == 4326
    first = adapter.read_snapshot("assignments", limit=1)
    second = adapter.read_snapshot("assignments", cursor=first.next_cursor, limit=1)
    third = adapter.read_snapshot("assignments", cursor=second.next_cursor, limit=1)
    rows = first.records + second.records + third.records
    assert [row.key for row in rows] == [{"tenant": "a", "assignment_id": i} for i in (1, 2, 3)]
    assert (
        rows[0].values["source"]
        == rows[1].values["source"]
        == {"resource_id": "Person", "key": {"employee_id": "e1"}}
    )
    assert (
        rows[0].values["target"]
        == rows[1].values["target"]
        == {"resource_id": "Project", "key": {"project_id": "p1"}}
    )
    assert third.complete and first.snapshot_id == third.snapshot_id
    assert not adapter.capabilities.consistent_snapshot


@pytest.mark.parametrize(
    "bad", ["null_relation", "duplicate_relation", "duplicate_endpoint", "mixed_endpoint"]
)
def test_live_neo4j_invalid_identity_rejects_read(live_graph, bad):
    adapter, driver = live_graph
    if bad == "null_relation":
        statement = "MATCH (a:Person {employee_id:'e1'}),(p:Project {project_id:'p1'}) CREATE (a)-[:`ASSIGNED``TO` {tenant:'a'}]->(p)"
    elif bad == "duplicate_relation":
        statement = "MATCH (a:Person {employee_id:'e1'}),(p:Project {project_id:'p1'}) CREATE (a)-[:`ASSIGNED``TO` {tenant:'a',assignment_id:1}]->(p)"
    elif bad == "duplicate_endpoint":
        statement = "CREATE (:Person {employee_id:'e1'})"
    else:
        statement = "CREATE (:Person {employee_id:99})"
    graph_command(driver, statement)
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("assignments")
    assert exc.value.code == ("unsupported_key_type" if bad == "mixed_endpoint" else "invalid_primary_key")


def test_live_mongo_extended_json_precision_missing_null_and_readonly_account(live_mongo):
    from pymongo.errors import OperationFailure

    adapter, _ = live_mongo
    assert adapter.test_connection().ok
    assert {r.resource_id for r in adapter.discover()} == {"records", "mixed"}
    first = adapter.read_snapshot("records", limit=1)
    last = adapter.read_snapshot("records", cursor=first.next_cursor, limit=1)
    values = first.records[0].values
    assert values["amount"] == {"$numberDecimal": "123456789.123456789123456789"}
    assert values["long"] == {"$numberLong": "9223372036854775807"}
    assert values["payload"]["$binary"]["base64"] == "AP8="
    assert values["nil"] is None and "nil" not in last.records[0].values
    assert last.complete and first.snapshot_id == last.snapshot_id
    assert first.consistency == "best_effort" and not adapter.capabilities.consistent_snapshot
    with pytest.raises(OperationFailure):
        adapter._db().records.insert_one({"_id": "forbidden"})


def test_live_mongo_mixed_bson_id_types_do_not_silently_disappear(live_mongo):
    adapter, _ = live_mongo
    page = adapter.read_snapshot("mixed", limit=1)
    rows = list(page.records)
    while page.next_cursor:
        page = adapter.read_snapshot("mixed", cursor=page.next_cursor, limit=1)
        rows.extend(page.records)
    assert len(rows) == 3
    assert rows[0].key == {"_id": {"$numberInt": "1"}}
    assert rows[1].key == {"_id": "a"}
    assert rows[2].key == {"_id": {"$oid": "000000000000000000000003"}}


def test_live_mongo_unknown_scope_and_closed_cursor_rejected(live_mongo):
    adapter, _ = live_mongo
    page = adapter.read_snapshot("records", limit=1)
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("outside")
    assert exc.value.code == "unknown_resource"
    adapter.close()
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("records", cursor=page.next_cursor)
    assert exc.value.code == "invalid_cursor"


def test_live_mongo_server_cursor_loss_requires_restart(live_mongo):
    adapter, database = live_mongo
    first = adapter.read_snapshot("mixed", limit=1)
    server_cursor_id = adapter._cursors[first.next_cursor]["cursor"].cursor_id
    assert server_cursor_id
    database.command("killCursors", "mixed", cursors=[server_cursor_id])
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("mixed", cursor=first.next_cursor, limit=1)
    assert exc.value.code == "snapshot_lost"
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("mixed", cursor=first.next_cursor)
    assert exc.value.code == "invalid_cursor"
    restarted = adapter.read_snapshot("mixed", limit=10)
    assert restarted.complete and len(restarted.records) == 3


def test_live_mongo_integer_width_and_embedded_id_field_order_are_stable(live_mongo):
    from bson import Int64

    adapter, database = live_mongo
    before = adapter.read_snapshot("mixed", limit=10).records[0]
    database.mixed.replace_one({"_id": 1}, {"_id": Int64(1)})
    after = adapter.read_snapshot("mixed", limit=10).records[0]
    assert before.key != after.key
    assert before.identity_key == after.identity_key == {"_id": {"type": "integer", "value": "1"}}
    database.mixed.insert_many([{"_id": {"a": 1, "b": 2}}, {"_id": {"b": 2, "a": 1}}])
    documents = [
        r
        for r in adapter.read_snapshot("mixed", limit=10).records
        if r.identity_key["_id"]["type"] == "bson_document"
    ]
    assert len(documents) == 2
    assert json.dumps(documents[0].identity_key, sort_keys=True) != json.dumps(
        documents[1].identity_key, sort_keys=True
    )


def test_live_mongo_ambiguous_numeric_id_is_rejected_without_losing_decimal_values(live_mongo):
    adapter, database = live_mongo
    database.mixed.insert_one({"_id": 1.5})
    with pytest.raises(ConnectorError) as exc:
        adapter.read_snapshot("mixed", limit=10)
    assert exc.value.code == "unsupported_key_semantics"
    assert not adapter._cursors
    assert (
        adapter.read_snapshot("records").records[0].values["amount"]["$numberDecimal"]
        == "123456789.123456789123456789"
    )
