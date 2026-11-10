"""Real API -> inbox -> leased job -> contributions, using native capture fixtures."""

import copy
import hashlib
import json
from uuid import uuid4

from ontology.cdc.projection import schedule_pending
from ontology.cdc.projection_models import CdcProjectionWindow
from ontology.connectors.base import ResourceSchema
from ontology.objects.contributions.models import ContributionRevision, SourceContribution
from ontology.objects.models import Entity, EntityRevision
from ontology.sources.ingestion import schema_fingerprint
from ontology.sources.models import Source, SourceIdentity, SourceRecordRevision
from ontology.worker import process_one
from sqlalchemy import func, select

from tests.support.paths import REPO_ROOT
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration

ROOT = REPO_ROOT


def fixtures(name="pg-toast-events.json"):
    data = json.loads((ROOT / "integrations/debezium/protocol" / name).read_text(encoding="utf-8"))
    return data["events"]


def setup_projection(
    platform, monkeypatch, *, empty=False, private_amount=False, duplicate_source_mapping=False
):
    client, app, owner, _ = platform
    resource = ResourceSchema(
        resource_id="public.items",
        name="items",
        namespace="public",
        primary_key=["id"],
        fields=[
            {"name": "id", "data_type": "bigint", "nullable": False},
            {"name": "name", "data_type": "text"},
            {"name": "amount", "data_type": "numeric"},
            {"name": "raw", "data_type": "bytea"},
            {"name": "optional", "data_type": "text"},
        ],
    ).model_dump(mode="json")
    resources = [resource]
    if empty:
        resources.append(
            ResourceSchema(
                resource_id="public.zz_empty",
                name="zz_empty",
                namespace="public",
                primary_key=["id"],
                fields=[{"name": "id", "data_type": "bigint", "nullable": False}],
            ).model_dump(mode="json")
        )
    with app.state.db.transaction(owner["tenant_id"]) as session:
        source = Source(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            name="Native CDC",
            kind="postgres",
            resources=resources,
        )
        session.add(source)
        session.flush()
        source_id = source.id
    definition = {
        "types": [
            {
                "key": "Item",
                "label": "Item",
                "properties": [
                    {"key": "source_id", "label": "ID", "data_type": "integer", "source_owned": True},
                    {"key": "name", "label": "Name", "data_type": "string", "source_owned": True},
                    {"key": "amount", "label": "Amount", "data_type": "number", "source_owned": True},
                    {"key": "optional", "label": "Optional", "data_type": "string", "source_owned": True},
                    {"key": "note", "label": "Manual", "data_type": "string"},
                ],
            }
        ],
        "mappings": [
            {
                "key": "items",
                "source_id": source_id,
                "resource_id": resource["resource_id"],
                "type_key": "Item",
                "schema_fingerprint": schema_fingerprint(resource),
                "fields": {
                    "source_id": {"source_field": "id"},
                    "name": {"source_field": "name"},
                    "amount": {"source_field": "amount"},
                    "optional": {"source_field": "optional"},
                },
            }
        ],
    }
    if empty:
        definition["mappings"].append(
            {
                "key": "empty",
                "source_id": source_id,
                "resource_id": "public.zz_empty",
                "type_key": "Item",
                "schema_fingerprint": schema_fingerprint(resources[1]),
                "fields": {"source_id": {"source_field": "id"}},
            }
        )
    if private_amount:
        definition["types"][0]["properties"][2]["read_roles"] = ["owner"]
    if duplicate_source_mapping:
        definition["types"][0]["properties"].append(
            {"key": "alias", "label": "Alias", "data_type": "string", "source_owned": True}
        )
        definition["mappings"][0]["fields"]["alias"] = {"source_field": "name"}
    release = publish(client, owner["project_id"], definition)["release_id"]
    response = client.post("/api/cdc/streams", json={"source_id": source_id, "expected_source_revision": 1})
    assert response.status_code == 201, response.text
    stream = response.json()["stream"]
    headers = {"Authorization": "Bearer " + response.json()["token"]}
    holder_id = str(uuid4())
    lease = client.post(
        f"/api/cdc/streams/{stream['id']}/lease", json={"holder_id": holder_id}, headers=headers
    ).json()
    response = client.post(
        f"/api/cdc/streams/{stream['id']}/projection",
        json={
            "expected_release_id": release,
            "expected_source_revision": 1,
            "resource_ids": [r["resource_id"] for r in resources],
        },
    )
    assert response.status_code == 200, response.text
    binding_id = response.json()["binding"]["id"]

    class Catalog:
        def discover(self):
            return [ResourceSchema.model_validate(r) for r in resources]

        def close(self):
            pass

    monkeypatch.setattr("ontology.cdc.projection.connector_for", lambda *_: Catalog())
    return {
        "platform": platform,
        "stream": stream,
        "source_id": source_id,
        "release": release,
        "resources": resources,
        "binding_id": binding_id,
        "headers": headers,
        "lease": {
            "generation": stream["generation"],
            "holder_id": holder_id,
            "fencing_token": lease["fencing_token"],
        },
    }


def append_events(state, events):
    events = copy.deepcopy(events)
    for event in events:
        material = {
            "identity_version": 1,
            "generation": state["stream"]["generation"],
            "source_partition": event["source_partition"],
            "source_position": {
                k: event["source_offset"][k]
                for k in ("lsn", "txId", "ts_usec", "messageType", "snapshot")
                if k in event["source_offset"]
            },
            "topic": event["topic"],
            "event_kind": event["event_kind"],
            "key_payload": event["key"]["payload"],
            "transaction": event["transaction"],
            "operation": event["operation"],
            "snapshot": event["snapshot"],
        }
        event["identity_material"] = json.dumps(
            material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        event["event_id"] = hashlib.sha256(event["identity_material"].encode()).hexdigest()
    response = state["platform"][0].post(
        f"/api/cdc/streams/{state['stream']['id']}/append",
        json={**state["lease"], "events": events},
        headers=state["headers"],
    )
    assert response.status_code == 200, response.text
    return response.json()


def view(state):
    response = state["platform"][0].get(f"/api/cdc/streams/{state['stream']['id']}/projection")
    assert response.status_code == 200, response.text
    return response.json()


def run(state, *, automatic=False):
    client, app, owner, _ = state["platform"]
    if automatic:
        assert schedule_pending(app.state.db, owner["tenant_id"])
    else:
        response = client.post(
            f"/api/cdc/streams/{state['stream']['id']}/project",
            json={"expected_binding_id": state["binding_id"]},
        )
        assert response.status_code == 202, response.text
    assert process_one(app.state.db, app.state.settings, "projection-test")
    return view(state)


def data_counts(state):
    _, app, owner, _ = state["platform"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        return {
            model.__tablename__: session.scalar(
                select(func.count()).select_from(model).where(model.project_id == owner["project_id"])
            )
            for model in (
                Entity,
                EntityRevision,
                SourceIdentity,
                SourceRecordRevision,
                SourceContribution,
                ContributionRevision,
                CdcProjectionWindow,
            )
        }
