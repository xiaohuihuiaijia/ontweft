"""Reviewed document facts enter the existing source-contribution machinery."""

import copy
import json
from collections import defaultdict
from types import SimpleNamespace

from sqlalchemy import func, select

from ontology.core.domain import active_definition, compare_and_swap
from ontology.core.errors import DomainError, conflict
from ontology.core.models import now
from ontology.identity.security import access, audit
from ontology.intelligence.proposal_models import AIProposal, DocumentRelationContribution
from ontology.modeling.models import ChangeSet, Release
from ontology.modeling.schemas import Definition
from ontology.modeling.validation import discard_validation
from ontology.objects.constraints import touch_entity
from ontology.objects.models import Entity, Relation
from ontology.sources.ingestion import fingerprint, project_snapshot, schema_fingerprint
from ontology.sources.models import Source, SourceIdentity, SourceRecordRevision, SourceSnapshot


def candidate_key(candidate):
    key = candidate.get("business_key") or {}
    if not key or any(value is None or value == "" for value in key.values()):
        raise DomainError(422, "candidate_identity_required", "选中的实例须填写明确且非空的业务键")
    return {"business_key": json.dumps(key, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)}


def selected_groups(proposal):
    groups = defaultdict(list)
    seen = set()
    for candidate in proposal.candidates:
        if not candidate.get("selected", True):
            continue
        identity = (candidate["type_key"], fingerprint(candidate_key(candidate)))
        if identity in seen:
            raise DomainError(422, "duplicate_candidate_identity", "同一类型的候选业务键重复，请先合并候选")
        seen.add(identity)
        groups[candidate["type_key"]].append(candidate)
    return groups


def prepare_document_source(session, proposal, user):
    """Prepare explicit mapping changes in the review draft; never publish it."""
    access(session, user, proposal.project_id, "sources:write")
    groups = selected_groups(proposal)
    if not groups:
        return None
    document_ids = sorted({item["id"] for item in proposal.inputs.get("documents", [])})
    if not document_ids:
        raise DomainError(422, "document_source_required", "文档实例候选须绑定选中的原始文档")
    origin_key = fingerprint(document_ids)
    source = session.scalar(select(Source).where(
        Source.tenant_id == proposal.tenant_id, Source.project_id == proposal.project_id,
        Source.kind == "document", Source.config["document_set"].as_string() == origin_key,
    ))
    if source is None:
        source = Source(
            tenant_id=proposal.tenant_id, project_id=proposal.project_id,
            name="文档业务事实", kind="document", status="discovered",
            config={"document_set": origin_key, "document_ids": document_ids}, resources=[], secret="",
        )
        session.add(source)
        session.flush()
    proposal.source_id = source.id
    definition = copy.deepcopy(proposal.definition)
    definition.setdefault("mappings", [])
    types = {value["key"]: value for value in definition["types"]}
    resources = copy.deepcopy(proposal.inputs.get("document_resources", source.resources))
    resource_by_id = {value["resource_id"]: value for value in resources}
    for type_key, candidates in groups.items():
        type_def = types.get(type_key)
        if not type_def:
            raise DomainError(422, "candidate_type_unknown", "候选实例引用了草稿中不存在的类型")
        props = {prop["key"]: prop for prop in type_def["properties"]}
        present = {key for candidate in candidates for key in candidate["properties"]}
        if present - props.keys():
            raise DomainError(422, "candidate_property_unknown", "候选实例包含未定义的属性")
        if not present:
            raise DomainError(422, "candidate_properties_empty", "选中的实例没有可导入属性")
        for key in present:
            # This is a visible draft change and must pass the ordinary model review.
            props[key]["source_owned"] = True
        resource_id = "document:" + type_key
        previous = resource_by_id.get(resource_id)
        field_keys = present | {field["name"] for field in previous["fields"]} if previous else present
        field_keys.discard("__business_key")
        if field_keys - props.keys():
            conflict("文档来源已发布字段不能直接移除，请先完成模型迁移")
        resource = {
            "resource_id": resource_id, "name": type_def["label"], "kind": "document",
            "primary_key": ["__business_key"], "foreign_keys": [],
            "fields": [{"name": "__business_key", "data_type": "string", "nullable": False,
                        "metadata": {"technical": True}}] + [
                {"name": key, "data_type": props[key]["data_type"], "nullable": True,
                 "metadata": {"label": props[key]["label"]}} for key in sorted(field_keys)
            ],
        }
        if previous != resource:
            if previous:
                resources[resources.index(previous)] = resource
            else:
                resources.append(resource)
        old_mapping = next((mapping for mapping in definition["mappings"] if (
            mapping["source_id"] == source.id and mapping["resource_id"] == resource_id
            and mapping["type_key"] == type_key
        )), None)
        identity_fields = []
        business_fields = set(candidates[0]["business_key"])
        if all(set(item["business_key"]) == business_fields for item in candidates):
            for unique_key in type_def.get("unique_keys", []):
                if set(unique_key) == business_fields and business_fields <= field_keys:
                    if any(any(item["properties"].get(key) != value for key, value in item["business_key"].items()) for item in candidates):
                        raise DomainError(422, "candidate_identity_mismatch", "候选业务键必须与对应属性值一致")
                    identity_fields = sorted(business_fields)
                    break
        mapping = {
            "key": old_mapping["key"] if old_mapping else "document_" + fingerprint([source.id, type_key])[:24],
            "source_id": source.id, "resource_id": resource_id, "type_key": type_key,
            "schema_fingerprint": schema_fingerprint(resource),
            "identity_fields": old_mapping.get("identity_fields", []) if old_mapping else identity_fields,
            "fields": {
                **{key: {"source_field": key, "transform": "identity"} for key in sorted(field_keys)},
                **(old_mapping["fields"] if old_mapping else {}),
            },
        }
        for key in field_keys:
            props[key]["source_owned"] = True
        if old_mapping:
            definition["mappings"][definition["mappings"].index(old_mapping)] = mapping
        else:
            definition["mappings"].append(mapping)
    # These resources belong to the reviewed draft until its ordinary publication.
    # In particular, another proposal must not change an active mapping's catalog.
    proposal.inputs = {**proposal.inputs, "document_resources": resources}
    proposal.definition = definition
    session.flush()
    return source


def draft_document_resources(session, change):
    """Only the proposal bound to this change can describe its pending document catalog."""
    proposal = session.scalar(select(AIProposal).where(
        AIProposal.tenant_id == change.tenant_id,
        AIProposal.project_id == change.project_id,
        AIProposal.changeset_id == change.id,
    ))
    if proposal is None or "document_resources" not in proposal.inputs:
        return {}  # Historical proposals already prepared their live source catalog.
    source = session.scalar(select(Source).where(
        Source.id == proposal.source_id, Source.tenant_id == change.tenant_id,
        Source.project_id == change.project_id, Source.kind == "document",
    ))
    if source is None:
        conflict("绑定的文档事实来源不可用")
    return {(source.id, resource["resource_id"]): resource for resource in proposal.inputs["document_resources"]}


def document_resources_fingerprint(resources):
    return fingerprint([
        {"source_id": source_id, "resource": resource}
        for (source_id, _resource_id), resource in sorted(resources.items())
    ])


def checked_document_resources(session, change, report):
    resources = draft_document_resources(session, change)
    if (resources or "document_resources_fingerprint" in report) and (
        document_resources_fingerprint(resources) != report.get("document_resources_fingerprint")
    ):
        conflict("文档来源描述已变化，请重新校验当前变更集")
    return resources


def publish_document_resources(session, change, report):
    """Apply only the approved document catalog in the model publication transaction."""
    resources = checked_document_resources(session, change, report)
    by_source = defaultdict(dict)
    for (source_id, resource_id), resource in resources.items():
        by_source[source_id][resource_id] = resource
    for source_id, updates in by_source.items():
        source = session.get(Source, source_id)
        catalog = {resource["resource_id"]: resource for resource in source.resources}
        catalog.update(updates)
        values = list(catalog.values())
        if values != source.resources:
            source.resources = copy.deepcopy(values)
            source.revision += 1


def document_mappings(proposal, definition):
    """Reviewed target properties must all have an actual published input binding."""
    mappings = {}
    for type_key, candidates in selected_groups(proposal).items():
        mapping = next((item for item in definition.get("mappings", []) if (
            item["source_id"] == proposal.source_id and item["resource_id"] == "document:" + type_key
            and item["type_key"] == type_key
        )), None)
        if not mapping:
            raise DomainError(409, "document_mapping_unpublished", "请先发布此次采纳的模型和文档映射")
        missing_fields = sorted({
            key for candidate in candidates for key in candidate["properties"]
            if key not in mapping["fields"] or mapping["fields"][key]["source_field"] != key
        })
        if missing_fields:
            raise DomainError(409, "document_candidate_unmapped", "候选属性尚无对应文档映射，请修改草稿并重新发布", {
                "type_key": type_key, "fields": missing_fields,
            })
        mappings[type_key] = mapping
    return mappings


def refresh_adopted_mapping(session, proposal, change, user):
    """Extend only the bound draft, preserving the original adopted proposal snapshot."""
    access(session, user, proposal.project_id, "ontology:write")
    if change.status == "published":
        release = session.get(Release, change.release_id)
        document_mappings(proposal, release.definition)
        return False
    previous_resources = proposal.inputs.get("document_resources")
    review = SimpleNamespace(
        tenant_id=proposal.tenant_id, project_id=proposal.project_id, inputs=proposal.inputs,
        definition=change.definition, candidates=proposal.candidates, source_id=proposal.source_id,
    )
    prepare_document_source(session, review, user)
    proposal.source_id = review.source_id
    proposal.inputs = review.inputs
    definition = Definition.model_validate(review.definition).model_dump()
    if definition == change.definition and proposal.inputs.get("document_resources") == previous_resources:
        return False
    discard_validation(session, change)
    compare_and_swap(session, change, change.revision, {
        "definition": definition, "status": "draft", "validation": {},
        "approved_by": None, "author_id": user.id,
    })
    audit(session, user, proposal.project_id, "ontology.edited", change.id, {"revision": change.revision})
    return True


def import_preconditions(session, proposal, project):
    """Shared review/publication gate, not authorization or candidate validation."""
    if proposal.imported:
        raise DomainError(409, "proposal_already_imported", "实例已导入，无需重复导入")
    if proposal.status != "adopted":
        raise DomainError(409, "document_mapping_required", "请先采纳候选")
    if not proposal.source_id:
        raise DomainError(409, "document_mapping_required", "此提案没有文档实例来源，无需在这里导入")
    source = session.scalar(select(Source).where(
        Source.id == proposal.source_id, Source.tenant_id == proposal.tenant_id,
        Source.project_id == project.id, Source.kind == "document",
    ))
    if source is None:
        raise DomainError(409, "document_source_unavailable", "文档事实来源不可用")
    if proposal.changeset_id:
        change = session.get(ChangeSet, proposal.changeset_id)
        if not change or change.status != "published":
            conflict("请先发布当前绑定草稿，再导入实例")
        if change.release_id != project.active_release_id:
            conflict("本体发布版本已改变，请基于当前发布版重新核对候选")
    elif proposal.baseline_release_id != project.active_release_id:
        conflict("本体发布版本已改变，请基于当前发布版重新核对候选")
    return source


def import_proposal(session, proposal, ctx):
    """Caller holds the project/proposal write lock; everything commits together."""
    access(session, ctx.user, proposal.project_id, "sources:write")
    if proposal.imported:
        return proposal.imported
    source = import_preconditions(session, proposal, ctx.project)
    definition = active_definition(session, ctx.project)
    groups = selected_groups(proposal)
    mappings = document_mappings(proposal, definition)
    snapshots, issues, counts, candidates_to_entities = [], [], {"created": 0, "updated": 0, "unchanged": 0, "blocked": 0}, {}
    job = SimpleNamespace(owner_id=ctx.user.id, tenant_id=proposal.tenant_id, project_id=proposal.project_id, checkpoint={})
    for type_key, candidates in groups.items():
        resource_id = "document:" + type_key
        generation = 1 + (session.scalar(select(func.max(SourceSnapshot.generation)).where(
            SourceSnapshot.source_id == source.id, SourceSnapshot.resource_id == resource_id
        )) or 0)
        snapshot = SourceSnapshot(
            tenant_id=proposal.tenant_id, project_id=proposal.project_id, source_id=source.id,
            source_revision=source.revision, resource_id=resource_id, generation=generation,
            schema_fingerprint=mappings[type_key]["schema_fingerprint"], release_id=ctx.project.active_release_id,
            status="captured", attempt=1, record_count=len(candidates), consistency="reviewed_subset", started_at=now(),
        )
        session.add(snapshot)
        session.flush()
        records = {}
        for candidate in candidates:
            key = candidate_key(candidate)
            previous = session.scalar(select(SourceIdentity).where(
                SourceIdentity.source_id == source.id, SourceIdentity.resource_id == resource_id,
                SourceIdentity.type_key == type_key, SourceIdentity.key_hash == fingerprint(key),
            ))
            previous_record = session.get(SourceRecordRevision, previous.last_record_id) if previous else None
            values = dict(previous_record.values or {}) if previous_record else {}
            values.update(candidate["properties"])
            values["__business_key"] = key["business_key"]
            field_evidence = dict(values.get("__ontweft_field_evidence", {}))
            field_evidence.update({key: candidate["evidence"] for key in candidate["properties"]})
            values["__ontweft_field_evidence"] = field_evidence
            values["__ontweft_proposal_id"] = proposal.id
            record = SourceRecordRevision(
                tenant_id=proposal.tenant_id, project_id=proposal.project_id,
                snapshot_id=snapshot.id, attempt=1, record_codec="snapshot-json-v1",
                source_key=key, key_hash=fingerprint(key), values=values,
                content_hash=fingerprint({"key": key, "identity_key": None, "values": values}),
            )
            records[candidate["id"]] = record
            session.add(record)
        session.flush()
        result = project_snapshot(session, job, snapshot)
        snapshot.projection, snapshot.status, snapshot.completed_at = result, "complete", now()
        snapshots.append(snapshot.id)
        issues.extend(result.get("issues", [])[:max(0, 100 - len(issues))])
        for key in counts:
            counts[key] += result.get(key, 0)
        for candidate in candidates:
            identity = session.scalar(select(SourceIdentity).where(
                SourceIdentity.source_id == source.id, SourceIdentity.resource_id == resource_id,
                SourceIdentity.type_key == type_key, SourceIdentity.key_hash == fingerprint(candidate_key(candidate)),
            ))
            entity = session.get(Entity, identity.entity_id) if identity and identity.last_record_id == records[candidate["id"]].id else None
            if entity and entity.data_status == "active" and not entity.deleted:
                candidates_to_entities[candidate["id"]] = entity
    relation_count, relation_issues = _import_relations(session, proposal, ctx, definition, candidates_to_entities)
    issues.extend(relation_issues[:max(0, 100 - len(issues))])
    result = {"imported": sum(counts[key] for key in ("created", "updated", "unchanged")),
              "snapshots": snapshots, "counts": counts, "relations": relation_count,
              "relations_blocked": len(relation_issues), "issues": issues}
    proposal.imported = result
    proposal.status = "imported"
    proposal.revision += 1
    return result


def _import_relations(session, proposal, ctx, definition, candidates):
    from ontology.objects.relation_history import (
        prepare_relation_properties,
        record_relation_revision,
        relation_values_equal,
    )

    types = {item["key"]: item for item in definition["types"]}
    count, issues = 0, []

    def blocked(candidate, code, message, relation_id=None):
        issues.append({"candidate_id": candidate["id"], "kind": "relation", "code": code, "message": message,
                       **({"relation_id": relation_id} if relation_id else {})})

    for candidate in proposal.relations:
        if not candidate.get("selected", True):
            continue
        source = candidates.get(candidate["source_candidate_id"])
        target = candidates.get(candidate["target_candidate_id"])
        if source is None or target is None:
            blocked(candidate, "relation_endpoint_unavailable", "关系端点未成功导入为有效对象，本次未导入此关系")
            continue
        relation_def = next((item for item in types[source.type_key].get("relations", []) if item["key"] == candidate["key"]), None)
        if not relation_def or relation_def["target_type"] != target.type_key:
            blocked(candidate, "invalid_candidate_relation", "候选关系与已发布类型不匹配")
            continue
        fact_key = fingerprint([source.id, candidate["key"], target.id])
        contribution = session.scalar(select(DocumentRelationContribution).where(
            DocumentRelationContribution.source_id == proposal.source_id,
            DocumentRelationContribution.fact_key == fact_key,
        ))
        if relation_def.get("cardinality") == "one":
            other = session.scalar(select(Relation.id).where(
                Relation.project_id == ctx.project.id, Relation.source_id == source.id,
                Relation.type_key == candidate["key"], Relation.target_id != target.id,
            ).limit(1))
            if other:
                blocked(candidate, "relation_cardinality_conflict", "候选关系与现有唯一目标冲突；已有关系保留，请在关系详情核对", other)
                continue
        row = session.scalar(select(Relation).where(
            Relation.project_id == ctx.project.id, Relation.source_id == source.id,
            Relation.target_id == target.id, Relation.type_key == candidate["key"],
        ))
        try:
            properties = prepare_relation_properties(
                relation_def, candidate.get("properties", {}), ctx, existing=row.properties if row else None,
            )
        except DomainError as exc:
            blocked(candidate, exc.code, exc.message, row.id if row else None)
            continue
        fields = {field["key"]: field for field in relation_def.get("properties", [])}
        if row and any(row.properties.get(key) is not None
                       and not relation_values_equal(fields[key], row.properties[key], properties.get(key))
                       for key in candidate.get("properties", {})):
            blocked(candidate, "relation_property_conflict", "候选条件与已有关系属性不同；原值和候选证据均保留，请在关系详情核对", row.id)
            continue
        if row:
            # Equivalent numbers preserve their existing representation and do not create a revision.
            for key, value in row.properties.items():
                if key in fields and relation_values_equal(fields[key], value, properties.get(key)):
                    properties[key] = value
        evidence = list(row.evidence) if row else []
        evidence.extend(ref for ref in candidate["evidence"] if ref not in evidence)
        if row is None:
            row = Relation(
                tenant_id=proposal.tenant_id, project_id=proposal.project_id,
                source_id=source.id, target_id=target.id, type_key=candidate["key"],
                properties=properties, evidence=evidence, manual=False,
            )
            session.add(row)
            touch_entity(session, source)
            session.flush()
            record_relation_revision(session, row, actor_id=ctx.user.id, model_release_id=ctx.project.active_release_id,
                                     reason="确认文档关系候选", operation="created")
        elif properties != row.properties or evidence != row.evidence:
            record_relation_revision(session, row, actor_id=ctx.user.id, model_release_id=ctx.project.active_release_id,
                                     reason="补充条件或证据前的关系")
            row.properties = properties
            row.evidence = evidence
            row.revision += 1
            record_relation_revision(session, row, actor_id=ctx.user.id, model_release_id=ctx.project.active_release_id,
                                     reason="确认文档关系候选并补充条件或证据")
            touch_entity(session, source)
        if not contribution:
            session.add(DocumentRelationContribution(
                tenant_id=proposal.tenant_id, project_id=proposal.project_id, source_id=proposal.source_id,
                fact_key=fact_key, proposal_id=proposal.id, relation_id=row.id, evidence=candidate["evidence"],
            ))
        else:
            contribution.evidence = list(contribution.evidence) + [
                ref for ref in candidate["evidence"] if ref not in contribution.evidence
            ]
        count += 1
    return count, issues
