"""Business-key reservations shared by manual edits and source materialization."""

import hashlib
import json
from decimal import Decimal

from sqlalchemy import delete, select, update

from ontology.core.errors import conflict
from ontology.identity.models import Project
from ontology.modeling.models import Release
from ontology.objects.models import EntityKey


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def number_text(value):
    # normalize() rounds to the current Decimal context (normally 28 digits).
    # Business identities must preserve every supplied digit.
    number = Decimal(str(value))
    if number.is_zero():
        return "0"
    result = format(number, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def key_values(definition, properties, fields):
    props = {p["key"]: p for p in definition["properties"]}
    values = []
    for field in sorted(fields):
        value = properties.get(field)
        if value is None or value == "":
            return None
        kind = props[field]["data_type"]
        if kind == "number":
            value = number_text(value)
        values.append([kind, value])
    return digest(sorted(fields)), digest(values)


def key_rows(entity, definition, definition_id):
    if entity.deleted or entity.data_status != "active":
        return []
    rows = []
    for fields in definition.get("unique_keys", []):
        key = key_values(definition, entity.properties, fields)
        if key:
            rows.append(
                {
                    "tenant_id": entity.tenant_id,
                    "project_id": entity.project_id,
                    "definition_id": definition_id,
                    "entity_id": entity.id,
                    "type_key": entity.type_key,
                    "fields_hash": key[0],
                    "value_hash": key[1],
                }
            )
    return rows


def sync_entity_keys(session, entity):
    project = session.get(Project, entity.project_id)
    release = session.get(Release, project.active_release_id) if project.active_release_id else None
    if not release:
        return
    definition = next((t for t in release.definition["types"] if t["key"] == entity.type_key), None)
    session.execute(
        delete(EntityKey).where(
            EntityKey.tenant_id == entity.tenant_id,
            EntityKey.project_id == entity.project_id,
            EntityKey.definition_id == release.id,
            EntityKey.entity_id == entity.id,
        )
    )
    for values in key_rows(entity, definition, release.id) if definition else []:
        existing = session.scalar(
            select(EntityKey.entity_id).where(
                *(
                    getattr(EntityKey, k) == values[k]
                    for k in (
                        "tenant_id",
                        "project_id",
                        "definition_id",
                        "type_key",
                        "fields_hash",
                        "value_hash",
                    )
                )
            )
        )
        if existing and existing != entity.id:
            conflict("组合业务键已被其他对象使用")
        session.add(EntityKey(**values))
    session.flush()


def touch_entity(session, entity):
    # Domain writes hold the project lock. The monotonic sequence avoids clock
    # skew and lets validation recheck only data committed since its baseline.
    seq = session.scalar(
        update(Project)
        .where(Project.id == entity.project_id, Project.tenant_id == entity.tenant_id)
        .values(data_revision=Project.data_revision + 1)
        .returning(Project.data_revision)
    )
    entity.changed_seq = seq
