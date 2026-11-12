"""AI drafts from a real bounded PostgreSQL sample; ingestion stays deterministic."""

import json

from ontology.intelligence.client import GenerationResult, ModelClient
from ontology.intelligence.prompts import MODEL_SCHEMA
from ontology.worker import process_one
from sqlalchemy import text
from tests.support.ingestion import capture
from tests.support.intake_database import intake_database as intake_database  # noqa: PLC0414
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


def test_selected_postgres_resource_to_ai_draft_publish_and_existing_import(
    pg_platform, intake_database, monkeypatch
):
    client, app, owner, _ = pg_platform
    fixture = intake_database
    with fixture["admin"].begin() as connection:
        connection.execute(
            text(f"COMMENT ON TABLE {fixture['schema']}.items IS 'Synthetic business inventory'")
        )
        connection.execute(
            text(f"COMMENT ON COLUMN {fixture['schema']}.items.group_code IS 'Owning group code'")
        )
        connection.execute(
            text(
                f"INSERT INTO {fixture['schema']}.items (id,name,group_code,amount) "
                "SELECT n,'Synthetic item ' || n,'001',0 FROM generate_series(3,16) n"
            )
        )
    app.state.settings.allowed_source_hosts = [fixture["config"]["host"]]
    app.state.settings.allow_private_source_addresses = True
    response = client.post(
        "/api/sources",
        json={
            "project_id": owner["project_id"],
            "name": "Synthetic inventory",
            "kind": "postgres",
            "config": fixture["config"],
            "credentials": fixture["credentials"],
        },
    )
    assert response.status_code == 201, response.text
    source = response.json()
    response = client.post(f"/api/sources/{source['id']}/discover")
    assert response.status_code == 200, response.text
    source = response.json()["source"]
    resources = {resource["name"]: resource for resource in source["resources"]}
    item = resources["items"]
    assert item["primary_key"] == ["id"]
    assert item["foreign_keys"][0]["target_resource"] == resources["groups"]["resource_id"]
    assert item["metadata"]["comment"] == "Synthetic business inventory"
    assert (
        next(field for field in item["fields"] if field["name"] == "group_code")["metadata"]["comment"]
        == "Owning group code"
    )
    response = client.post(
        "/api/model-connections",
        json={
            "name": "Synthetic compatible model",
            "base_url": "https://models.example.test/v1",
        },
    )
    assert response.status_code == 201, response.text
    connection_id = response.json()["id"]
    calls = []

    def generate(_client, role, *, schema, text, before_request=None, **kwargs):
        assert role == "synthesis" and schema == MODEL_SCHEMA
        payload = json.loads(text)
        calls.append(payload)
        materials = payload["materials"]
        assert len(materials) == 1 and "database" in materials[0]
        profile = materials[0]["database"]
        assert profile["source_id"] == source["id"]
        assert profile["resource"]["resource_id"] == item["resource_id"]
        assert len(profile["samples"]) == 12
        assert profile["resource"]["metadata"]["comment"] == "Synthetic business inventory"
        assert "合成分组甲" not in text  # An unselected table is discovered, never sampled for this request.
        if before_request:
            before_request({"estimated_cost": 0.01})
        data = {
            "types": [
                {
                    "key": "Inventory",
                    "label": "业务库存",
                    "description": "From the selected inventory table",
                    "properties": [
                        {
                            "key": key,
                            "label": key,
                            "data_type": kind,
                            "required": False,
                            "enum": [],
                            "unit": None,
                            "source_owned": True,
                        }
                        for key, kind in (("id", "integer"), ("name", "string"), ("group_code", "string"))
                    ],
                    "unique_keys": [["id"]],
                    "relations": [],
                }
            ],
            "mappings": [
                {
                    "source_id": source["id"],
                    "resource_id": item["resource_id"],
                    "type_key": "Inventory",
                    "fields": [
                        {"target": key, "source_field": key, "transform": "identity"}
                        for key in ("id", "name", "group_code")
                    ],
                }
            ],
            "gaps": [],
        }
        result = GenerationResult(
            data, "synthetic-model", {"input_tokens": 100, "output_tokens": 50}, cost=0.01
        )
        if kwargs.get("after_response"):
            kwargs["after_response"]({"usage": result.usage, "cost": result.cost,
                                      "model": result.model, "elapsed_ms": result.elapsed_ms})
        return result

    monkeypatch.setattr(ModelClient, "generate", generate)
    response = client.post(
        "/api/ai/proposals",
        json={
            "project_id": owner["project_id"],
            "connection_id": connection_id,
            "document_ids": [],
            "source_selections": [{"source_id": source["id"], "resource_ids": [item["resource_id"]]}],
            "instructions": "只创建库存类型及其来源映射，不采集未选择的分组表",
            "budget_cny": 1,
        },
    )
    assert response.status_code == 202, response.text
    proposal = response.json()
    assert process_one(app.state.db, app.state.settings)
    proposal = client.get(f"/api/ai/proposals/{proposal['id']}").json()
    assert proposal["status"] == "ready", proposal["job"]
    assert len(calls) == 1 and proposal["candidates"] == [] and proposal["relations"] == []
    assert len(proposal["definition"]["mappings"]) == 1
    assert proposal["definition"]["mappings"][0]["identity_fields"] == []
    response = client.post(
        f"/api/ai/proposals/{proposal['id']}/adopt", json={"expected_revision": proposal["revision"]}
    )
    assert response.status_code == 200, response.text
    change = response.json()["changeset"]
    assert response.json()["proposal"]["source_id"] is None
    for operation in ("validate", "approve", "publish"):
        response = client.post(
            f"/api/changesets/{change['id']}/{operation}", json={"expected_revision": change["revision"]}
        )
        assert response.status_code == 200, response.text
        change = response.json()
    result = capture(client, app, source, item)
    assert result["snapshot"]["status"] == "complete", result
    assert result["snapshot"]["projection"]["created"] == 16
    assert len(calls) == 1  # The regular snapshot import, including unseen rows, never invokes the model.
    objects = client.get("/api/objects", params={"project_id": owner["project_id"]}).json()
    assert objects["total"] == 16
    assert {row["type_key"] for row in objects["items"]} == {"Inventory"}
