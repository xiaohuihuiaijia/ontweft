import sqlite3

from ontology.worker import process_one

from tests.support.platform import publish


def make_source(client, app, project_id):
    root = app.state.settings.data_dir / "imports"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "inventory.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE inventory (id INTEGER PRIMARY KEY, product TEXT NOT NULL, quantity TEXT)"
        )
        connection.executemany("INSERT INTO inventory VALUES (?, ?, ?)", [(1, " A ", "80"), (2, "B", "20")])
    response = client.post(
        "/api/sources",
        json={
            "project_id": project_id,
            "name": "库存数据库",
            "kind": "sqlite",
            "config": {"database": str(path)},
        },
    )
    assert response.status_code == 201, response.text
    source = response.json()
    assert client.post(f"/api/sources/{source['id']}/test").json()["ok"]
    discovery = client.post(f"/api/sources/{source['id']}/discover")
    assert discovery.status_code == 200, discovery.text
    resource = discovery.json()["resources"][0]
    definition = {
        "types": [
            {
                "key": "Stock",
                "label": "库存",
                "properties": [
                    {
                        "key": "name",
                        "label": "名称",
                        "data_type": "string",
                        "required": True,
                        "source_owned": True,
                    },
                    {"key": "quantity", "label": "数量", "data_type": "integer", "source_owned": True},
                    {"key": "note", "label": "人工备注", "data_type": "string"},
                ],
            }
        ],
        "mappings": [
            {
                "key": "inventory_to_stock",
                "source_id": source["id"],
                "resource_id": resource["resource_id"],
                "schema_fingerprint": resource["schema_fingerprint"],
                "type_key": "Stock",
                "fields": {
                    "name": {"source_field": "product", "transform": "trim"},
                    "quantity": {"source_field": "quantity", "transform": "integer"},
                },
            }
        ],
    }
    publish(client, project_id, definition)
    return source, resource, path, definition


def capture(client, app, source, resource):
    response = client.post(
        f"/api/sources/{source['id']}/snapshots",
        json={"expected_source_revision": source["revision"], "resource_id": resource["resource_id"]},
    )
    assert response.status_code == 201, response.text
    snapshot = response.json()["snapshot"]
    assert process_one(app.state.db, app.state.settings)
    result = client.get(f"/api/snapshots/{snapshot['id']}/records")
    assert result.status_code == 200, result.text
    return result.json()
