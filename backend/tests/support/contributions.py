from ontology.cdc.models import CdcEvent
from ontology.core.domain import save_entity_revision
from ontology.identity.models import Project, User
from ontology.identity.security import access
from ontology.modeling.models import ChangeSet, Release
from ontology.modeling.schemas import Definition
from ontology.objects.contributions.service import replace_contribution
from ontology.objects.models import Entity
from ontology.sources.ingestion import fingerprint, schema_fingerprint
from ontology.sources.models import Source, SourceIdentity, SourceRecordRevision, SourceSnapshot

from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def raw_record(session, owner, source, release, values, generation=1):
    resource = source.resources[0]
    snapshot = SourceSnapshot(
        tenant_id=owner["tenant_id"],
        project_id=owner["project_id"],
        source_id=source.id,
        source_revision=source.revision,
        resource_id=resource["resource_id"],
        generation=generation,
        schema_fingerprint=schema_fingerprint(resource),
        release_id=release.id,
        status="complete",
        attempt=1,
        record_count=1,
        consistency="transaction",
    )
    session.add(snapshot)
    session.flush()
    key = {"id": 1}
    record = SourceRecordRevision(
        tenant_id=owner["tenant_id"],
        project_id=owner["project_id"],
        snapshot_id=snapshot.id,
        record_codec="snapshot-json-v1",
        attempt=1,
        source_key=key,
        key_hash=fingerprint(key),
        values=values,
        content_hash=fingerprint({"key": key, "identity_key": None, "values": values}),
    )
    session.add(record)
    session.flush()
    return record


def seed(db, owner):
    with db.transaction(owner["tenant_id"]) as session:
        resource = {
            "resource_id": "inventory",
            "kind": "table",
            "fields": [{"name": "name"}, {"name": "quantity"}],
            "primary_key": ["id"],
            "foreign_keys": [],
        }
        sources = [
            Source(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                name=f"Source {i}",
                kind="postgres",
                resources=[resource],
            )
            for i in range(2)
        ]
        session.add_all(sources)
        session.flush()
        definition = Definition.model_validate(
            {
                "types": [
                    {
                        "key": "Stock",
                        "label": "Stock",
                        "properties": [
                            {
                                "key": "name",
                                "label": "Name",
                                "data_type": "string",
                                "source_owned": True,
                                "required": True,
                            },
                            {
                                "key": "quantity",
                                "label": "Quantity",
                                "data_type": "number",
                                "source_owned": True,
                            },
                            {"key": "note", "label": "Manual", "data_type": "string"},
                        ],
                    }
                ],
                "mappings": [
                    {
                        "key": f"mapping{i}",
                        "source_id": source.id,
                        "resource_id": "inventory",
                        "type_key": "Stock",
                        "schema_fingerprint": schema_fingerprint(resource),
                        "fields": {
                            "name": {"source_field": "name"},
                            "quantity": {"source_field": "quantity"},
                        },
                    }
                    for i, source in enumerate(sources)
                ],
            }
        ).model_dump()
        change = ChangeSet(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            title="Fixed evidence",
            definition=definition,
            author_id=owner["user_id"],
            status="published",
        )
        session.add(change)
        session.flush()
        release = Release(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            version=1,
            definition=definition,
            changeset_id=change.id,
            published_by=owner["user_id"],
        )
        session.add(release)
        session.flush()
        project = session.get(Project, owner["project_id"])
        project.active_release_id, project.version = release.id, 1
        record = raw_record(session, owner, sources[0], release, {"name": "A", "quantity": "10.00"})
        entity = Entity(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            type_key="Stock",
            properties={"name": "A", "quantity": "10.00", "note": "manual retained"},
            release_id=release.id,
            evidence=[{"source_record_id": record.id, "fields": ["name", "quantity"]}],
        )
        session.add(entity)
        session.flush()
        identity = SourceIdentity(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            source_id=sources[0].id,
            resource_id="inventory",
            type_key="Stock",
            entity_id=entity.id,
            key_hash=record.key_hash,
            key_codec="legacy-json-v1",
            last_record_id=record.id,
            mapping_key="mapping0",
        )
        session.add(identity)
        session.flush()
        save_entity_revision(session, entity, session.get(User, owner["user_id"]))
        return {
            "entity": entity.id,
            "identity": identity.id,
            "release": release.id,
            "record": record.id,
            "sources": [row.id for row in sources],
        }


def loaded(session, owner, ids):
    ctx = access(session, session.get(User, owner["user_id"]), owner["project_id"], "sources:write")
    entity, identity, release = (
        session.get(Entity, ids["entity"]),
        session.get(SourceIdentity, ids["identity"]),
        session.get(Release, ids["release"]),
    )
    return ctx, entity, identity, release


def apply(session, ctx, identity, record, release, index=0, values=None, **kwargs):
    return replace_contribution(
        session,
        ctx,
        identity,
        record,
        release.id,
        release.definition["mappings"][index],
        release.definition["types"][0],
        values,
        **kwargs,
    )


def native_record(session, owner, stream, sequence):
    event = CdcEvent(
        tenant_id=owner["tenant_id"],
        project_id=owner["project_id"],
        stream_id=stream.id,
        sequence=sequence,
        event_id=fingerprint([stream.id, sequence]),
        identity_hash=fingerprint([stream.id, sequence]),
        record_hash=fingerprint({"sequence": sequence}),
        record={"sequence": sequence},
    )
    session.add(event)
    session.flush()
    record = SourceRecordRevision(
        tenant_id=owner["tenant_id"],
        project_id=owner["project_id"],
        record_codec="cdc-native-reference-v1",
        cdc_event_id=event.id,
    )
    session.add(record)
    session.flush()
    return record
