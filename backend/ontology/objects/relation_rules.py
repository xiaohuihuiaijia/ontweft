"""Persistent exact-key relationships, with owned contributions and coalesced work."""

from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import delete, func, select, update

from ontology.core.domain import active_definition, row_dict, scoped_record
from ontology.core.errors import DomainError, conflict
from ontology.core.models import now, uid
from ontology.core.schemas import RevisionInput, StrictModel
from ontology.identity.models import User
from ontology.identity.security import access, audit, authenticated
from ontology.jobs.models import Job
from ontology.jobs.service import check_job_authority, fenced_job
from ontology.objects.constraints import digest, number_text, touch_entity
from ontology.objects.models import Entity, Relation
from ontology.objects.relation_matching import MatchRule, _family, _property, _value_key
from ontology.objects.rule_models import RelationContribution, RelationMatchIndex, RelationRule, RelationWork

router = APIRouter()


def definition_for(session, ctx, config):
    definitions = {t["key"]: t for t in active_definition(session, ctx.project)["types"]}
    source = definitions.get(config["source_type"])
    relation = next(
        (r for r in (source or {}).get("relations", []) if r["key"] == config["relation_key"]), None
    )
    target = definitions.get(relation["target_type"]) if relation else None
    if not source or not target:
        conflict("规则引用的关系或类型已改变，请修订规则")
    left = _property(source, config["source_field"], ctx.role)
    right = _property(target, config["target_field"], ctx.role)
    if _family(left) != _family(right):
        conflict("规则两侧字段类型不兼容")
    return source, target, relation, _family(left)


def enqueue_rule(session, rule):
    job = session.get(Job, rule.job_id) if rule.job_id else None
    if job and job.status in {"pending", "running"}:
        return
    job = Job(
        tenant_id=rule.tenant_id,
        project_id=rule.project_id,
        owner_id=rule.owner_id,
        kind="relation.recompute",
        payload={"rule_id": rule.id},
    )
    session.add(job)
    session.flush()
    rule.job_id = job.id


def queue_entities(session, rule, ids):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    for entity_id in set(ids):
        statement = insert(RelationWork).values(
            id=uid(),
            tenant_id=rule.tenant_id,
            project_id=rule.project_id,
            created_at=now(),
            rule_id=rule.id,
            entity_id=entity_id,
            status="pending",
            error=None,
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["rule_id", "entity_id"], set_={"status": "pending", "error": None}
            )
        )


def index_entity(session, ctx, rule, entity, definition):
    source, target, _, family = definition
    old = session.scalars(
        select(RelationMatchIndex).where(
            RelationMatchIndex.rule_id == rule.id, RelationMatchIndex.entity_id == entity.id
        )
    ).all()
    affected = {r.value_hash for r in old if r.side == "target" and r.value_hash}
    session.execute(
        delete(RelationMatchIndex).where(
            RelationMatchIndex.rule_id == rule.id, RelationMatchIndex.entity_id == entity.id
        )
    )
    for side, type_def, field in [
        ("source", source, rule.config["source_field"]),
        ("target", target, rule.config["target_field"]),
    ]:
        if entity.type_key != type_def["key"]:
            continue
        value = (
            _value_key(entity.properties.get(field), family)
            if not entity.deleted and entity.data_status == "active"
            else None
        )
        value_hash = (
            digest([value[0], number_text(value[1]) if isinstance(value[1], Decimal) else value[1]])
            if value
            else None
        )
        session.add(
            RelationMatchIndex(
                tenant_id=rule.tenant_id,
                project_id=rule.project_id,
                rule_id=rule.id,
                entity_id=entity.id,
                side=side,
                value_hash=value_hash,
            )
        )
        if side == "source":
            queue_entities(session, rule, [entity.id])
        elif value_hash:
            affected.add(value_hash)
    session.flush()
    if affected:
        ids = session.scalars(
            select(RelationMatchIndex.entity_id).where(
                RelationMatchIndex.rule_id == rule.id,
                RelationMatchIndex.side == "source",
                RelationMatchIndex.value_hash.in_(affected),
            )
        )
        queue_entities(session, rule, ids)


def changed_entity(session, entity):
    rules = session.scalars(
        select(RelationRule).where(
            RelationRule.tenant_id == entity.tenant_id,
            RelationRule.project_id == entity.project_id,
            RelationRule.enabled.is_(True),
        )
    ).all()
    for rule in rules:
        try:
            ctx = access(session, session.get(User, rule.owner_id), rule.project_id, "objects:write")
            definition = definition_for(session, ctx, rule.config)
        except DomainError as exc:
            rule.enabled, rule.error = False, exc.message
            continue
        if entity.type_key not in {definition[0]["key"], definition[1]["key"]}:
            continue
        index_entity(session, ctx, rule, entity, definition)
        enqueue_rule(session, rule)


def remove_contribution(session, contribution):
    relation = session.get(Relation, contribution.relation_id)
    session.delete(contribution)
    session.flush()
    supported = session.scalar(
        select(RelationContribution.id).where(RelationContribution.relation_id == relation.id).limit(1)
    )
    from ontology.intelligence.proposal_models import DocumentRelationContribution

    supported = supported or session.scalar(
        select(DocumentRelationContribution.id)
        .where(DocumentRelationContribution.relation_id == relation.id).limit(1)
    )
    if not relation.manual and not supported:
        touch_entity(session, session.get(Entity, relation.source_id))
        session.delete(relation)


def recompute(session, ctx, rule, work, definition):
    source_def, _, relation_def, _ = definition
    source = session.get(Entity, work.entity_id)
    existing = session.scalar(
        select(RelationContribution).where(
            RelationContribution.rule_id == rule.id, RelationContribution.source_id == source.id
        )
    )
    if source.data_status in {"conflict", "incomplete"}:
        return "来源对象存在贡献冲突或字段缺失"
    value = session.scalar(
        select(RelationMatchIndex.value_hash).where(
            RelationMatchIndex.rule_id == rule.id,
            RelationMatchIndex.entity_id == source.id,
            RelationMatchIndex.side == "source",
        )
    )
    targets = (
        session.scalars(
            select(RelationMatchIndex.entity_id)
            .where(
                RelationMatchIndex.rule_id == rule.id,
                RelationMatchIndex.side == "target",
                RelationMatchIndex.value_hash == value,
                True if rule.config.get("allow_self") else RelationMatchIndex.entity_id != source.id,
            )
            .limit(2)
        ).all()
        if value
        and source.type_key == source_def["key"]
        and not source.deleted
        and source.data_status == "active"
        else []
    )
    if len(targets) > 1:
        return "业务键匹配多个目标，保留原关系并暂停此记录"
    if existing:
        previous = session.get(Relation, existing.relation_id)
        target = session.get(Entity, previous.target_id)
        if target.data_status in {"conflict", "incomplete"}:
            return "原目标存在贡献冲突，保留关系贡献等待解决"
        if targets and previous.target_id == targets[0] and previous.type_key == rule.config["relation_key"]:
            return None
    if targets and relation_def.get("cardinality") == "one":
        incompatible = session.scalar(
            select(Relation.id)
            .where(
                Relation.tenant_id == rule.tenant_id,
                Relation.project_id == rule.project_id,
                Relation.source_id == source.id,
                Relation.type_key == rule.config["relation_key"],
                Relation.target_id != targets[0],
                Relation.id != (existing.relation_id if existing else ""),
            )
            .limit(1)
        )
        if incompatible:
            return "其他关系已占用唯一目标，保留原关系等待解决"
    if existing:
        old_relation = session.get(Relation, existing.relation_id)
        if targets and old_relation.target_id != targets[0] and relation_def.get("cardinality") == "one":
            other_support = session.scalar(
                select(RelationContribution.id)
                .where(
                    RelationContribution.relation_id == old_relation.id,
                    RelationContribution.id != existing.id,
                )
                .limit(1)
            )
            from ontology.intelligence.proposal_models import DocumentRelationContribution

            other_support = other_support or session.scalar(
                select(DocumentRelationContribution.id)
                .where(DocumentRelationContribution.relation_id == old_relation.id).limit(1)
            )
            if old_relation.manual or other_support:
                return "原关系仍有人工作用或其他来源贡献，不能覆盖唯一目标"
        remove_contribution(session, existing)
        session.flush()
    if targets:
        relation = session.scalar(
            select(Relation).where(
                Relation.tenant_id == rule.tenant_id,
                Relation.project_id == rule.project_id,
                Relation.source_id == source.id,
                Relation.target_id == targets[0],
                Relation.type_key == rule.config["relation_key"],
            )
        )
        if not relation:
            relation = Relation(
                tenant_id=rule.tenant_id,
                project_id=rule.project_id,
                source_id=source.id,
                target_id=targets[0],
                type_key=rule.config["relation_key"],
                properties={},
                evidence=[],
                manual=False,
            )
            session.add(relation)
            touch_entity(session, source)
            session.flush()
        session.add(
            RelationContribution(
                tenant_id=rule.tenant_id,
                project_id=rule.project_id,
                rule_id=rule.id,
                source_id=source.id,
                relation_id=relation.id,
            )
        )
    return None


def run_rule(db, run):
    with db.transaction(run["tenant_id"], write=True) as session:
        job = fenced_job(session, run)
        rule = session.get(RelationRule, job.payload["rule_id"])
        if not rule.enabled:
            job.status, job.lease_until = "succeeded", 0
            return
        try:
            check_job_authority(session, job)
            ctx = access(session, session.get(User, rule.owner_id), rule.project_id, "objects:write")
            definition = definition_for(session, ctx, rule.config)
        except DomainError as exc:
            rule.enabled, rule.error = False, exc.message
            job.status, job.error, job.lease_until = "failed", exc.message, 0
            return
        if not rule.indexed:
            rows = session.scalars(
                select(Entity)
                .where(
                    Entity.tenant_id == rule.tenant_id,
                    Entity.project_id == rule.project_id,
                    Entity.type_key.in_([definition[0]["key"], definition[1]["key"]]),
                    Entity.id > rule.cursor,
                )
                .order_by(Entity.id)
                .limit(250)
            ).all()
            for entity in rows:
                index_entity(session, ctx, rule, entity, definition)
            if rows:
                rule.cursor = rows[-1].id
            if len(rows) < 250:
                rule.indexed = True
        else:
            work = session.scalars(
                select(RelationWork)
                .where(RelationWork.rule_id == rule.id, RelationWork.status == "pending")
                .order_by(RelationWork.id)
                .limit(100)
            ).all()
            for item in work:
                message = recompute(session, ctx, rule, item, definition)
                if message:
                    item.status, item.error = "blocked", message
                else:
                    session.delete(item)
        session.flush()
        pending = session.scalar(
            select(func.count())
            .select_from(RelationWork)
            .where(RelationWork.rule_id == rule.id, RelationWork.status == "pending")
        )
        blocked = session.scalar(
            select(func.count())
            .select_from(RelationWork)
            .where(RelationWork.rule_id == rule.id, RelationWork.status == "blocked")
        )
        job.result = {"rule_id": rule.id, "indexed": rule.indexed, "pending": pending, "blocked": blocked}
        job.status = "succeeded" if rule.indexed and not pending else "pending"
        job.lease_until, job.updated_at = 0, now()


class RuleInput(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    rule: MatchRule


class RuleUpdate(RevisionInput):
    enabled: bool
    rule: MatchRule | None = None


@router.get("/relation-rules")
def rules(request: Request, project_id: str, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        access(session, user, project_id, "objects:write")
        rows = session.scalars(
            select(RelationRule).where(
                RelationRule.tenant_id == user.tenant_id, RelationRule.project_id == project_id
            )
        ).all()
        result = []
        for row in rows:
            issues = session.scalars(
                select(RelationWork)
                .where(RelationWork.rule_id == row.id, RelationWork.status == "blocked")
                .limit(25)
            ).all()
            job = session.get(Job, row.job_id) if row.job_id else None
            result.append(
                {
                    **row_dict(row),
                    "issues": [row_dict(i) for i in issues],
                    "job": row_dict(job) if job else None,
                }
            )
        return {"items": result}


@router.post("/relation-rules", status_code=201)
def create_rule(data: RuleInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        ctx = access(session, user, data.rule.project_id, "objects:write")
        if data.rule.expected_model_release_id != ctx.project.active_release_id:
            conflict("模型已改变，请刷新规则")
        config = data.rule.model_dump()
        definition_for(session, ctx, config)
        rule = RelationRule(
            tenant_id=user.tenant_id,
            project_id=ctx.project.id,
            name=data.name,
            config=config,
            owner_id=user.id,
        )
        session.add(rule)
        session.flush()
        enqueue_rule(session, rule)
        audit(session, user, ctx.project.id, "relation.rule.created", rule.id)
        return row_dict(rule)


@router.patch("/relation-rules/{rule_id}")
def update_rule(rule_id: str, data: RuleUpdate, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        rule = scoped_record(session, RelationRule, rule_id, user)
        ctx = access(session, user, rule.project_id, "objects:write")
        session.refresh(rule)
        if rule.revision != data.expected_revision:
            conflict("规则修订已改变")
        if data.rule:
            if (
                data.rule.project_id != rule.project_id
                or data.rule.expected_model_release_id != ctx.project.active_release_id
            ):
                conflict("规则项目或模型基线不匹配")
            definition_for(session, ctx, data.rule.model_dump())
            rule.config = data.rule.model_dump()
        rule.enabled, rule.owner_id, rule.error = data.enabled, user.id, None
        rule.revision += 1
        if data.enabled:
            definition_for(session, ctx, rule.config)
            rule.cursor, rule.indexed = "", False
            session.execute(delete(RelationMatchIndex).where(RelationMatchIndex.rule_id == rule.id))
            ids = session.scalars(
                select(RelationContribution.source_id).where(RelationContribution.rule_id == rule.id)
            )
            queue_entities(session, rule, ids)
            session.execute(
                update(RelationWork)
                .where(RelationWork.rule_id == rule.id)
                .values(status="pending", error=None)
            )
            previous = session.get(Job, rule.job_id) if rule.job_id else None
            if previous and previous.status in {"pending", "running"}:
                previous.status = "cancelled"
            enqueue_rule(session, rule)
        audit(
            session,
            user,
            rule.project_id,
            "relation.rule.updated",
            rule.id,
            {"revision": rule.revision, "enabled": rule.enabled},
        )
        return row_dict(rule)
