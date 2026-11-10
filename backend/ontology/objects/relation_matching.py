"""Explicit, version-bound relation matching over already authorized typed objects."""

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import select

from ontology.core.domain import active_definition, wire_value
from ontology.core.errors import DomainError, conflict, forbidden
from ontology.core.models import uid
from ontology.core.schemas import StrictModel
from ontology.identity.security import access, allowed_field, audit, authenticated
from ontology.objects.models import Entity, Relation
from ontology.sources.ingestion import fingerprint

router = APIRouter()
MAX_OBJECTS = 10000
MAX_EXISTING_RELATIONS = 20000
SAMPLE_SIZE = 25


class MatchRule(StrictModel):
    project_id: str
    expected_model_release_id: str = Field(min_length=1, max_length=36)
    source_type: str = Field(min_length=1, max_length=120)
    relation_key: str = Field(min_length=1, max_length=80)
    source_field: str = Field(min_length=1, max_length=80)
    target_field: str = Field(min_length=1, max_length=80)
    allow_self: bool = False


class ApplyMatch(StrictModel):
    rule: MatchRule
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    allow_partial: bool = False


class MatchIssuePage(StrictModel):
    rule: MatchRule
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    offset: int = Field(default=0, ge=0, le=MAX_OBJECTS)
    limit: int = Field(default=SAMPLE_SIZE, ge=1, le=100)


def _property(type_def, key, role):
    prop = next((p for p in type_def["properties"] if p["key"] == key), None)
    if not prop or not allowed_field(prop, role):
        forbidden()
    if prop["data_type"] == "json":
        raise DomainError(422, "unsupported_match", "请选择明确的标量字段，JSON 不用于隐式关系匹配")
    return prop


def _family(prop):
    return "text" if prop["data_type"] in {"string", "enum"} else prop["data_type"]


def _value_key(value, family):
    if value is None or value == "":
        return None
    if family == "number":
        # Decimal equality preserves exact value without a floating point round trip.
        try:
            value = Decimal(str(value))
            if not value.is_finite():
                raise InvalidOperation()
        except (ValueError, InvalidOperation):
            raise DomainError(422, "invalid_match_value", "对象包含无效十进制值，请先修正数据") from None
    return family, value


def matching_plan(session, ctx, rule, *, issue_offset=0, issue_limit=SAMPLE_SIZE):
    session.refresh(ctx.project, attribute_names=["active_release_id"])
    if rule.expected_model_release_id != ctx.project.active_release_id:
        conflict("模型版本已改变，请重新选择关系并预览")
    definitions = {t["key"]: t for t in active_definition(session, ctx.project)["types"]}
    source_type = definitions.get(rule.source_type)
    relation = next(
        (r for r in (source_type or {}).get("relations", []) if r["key"] == rule.relation_key), None
    )
    target_type = definitions.get(relation["target_type"]) if relation else None
    if not source_type or not target_type:
        raise DomainError(422, "undefined_relation", "请先定义并发布关系、方向及目标类型")
    source_prop = _property(source_type, rule.source_field, ctx.role)
    target_prop = _property(target_type, rule.target_field, ctx.role)
    family = _family(source_prop)
    if family != _family(target_prop):
        raise DomainError(422, "incompatible_match", "两侧字段类型不兼容，请先显式修正模型或来源转换")

    def objects(type_key):
        items = session.scalars(
            select(Entity)
            .where(
                Entity.tenant_id == ctx.user.tenant_id,
                Entity.project_id == ctx.project.id,
                Entity.type_key == type_key,
                Entity.deleted.is_(False),
                Entity.data_status == "active",
            )
            .order_by(Entity.id)
            .limit(MAX_OBJECTS + 1)
        ).all()
        if len(items) > MAX_OBJECTS:
            raise DomainError(422, "match_budget", "本次匹配任一类型超过 10,000 个对象；未截断或执行部分写入")
        return items

    sources = objects(source_type["key"])
    targets = sources if source_type["key"] == target_type["key"] else objects(target_type["key"])
    index = {}
    for target in targets:
        key = _value_key(target.properties.get(rule.target_field), family)
        if key is not None:
            index.setdefault(key, []).append(target)
    existing = (
        session.scalars(
            select(Relation)
            .where(
                Relation.tenant_id == ctx.user.tenant_id,
                Relation.project_id == ctx.project.id,
                Relation.type_key == rule.relation_key,
                Relation.source_id.in_([row.id for row in sources]),
            )
            .order_by(Relation.id)
            .limit(MAX_EXISTING_RELATIONS + 1)
        ).all()
        if sources
        else []
    )
    if len(existing) > MAX_EXISTING_RELATIONS:
        raise DomainError(422, "match_budget", "已有关系超出本次匹配预算；未执行写入")
    linked = {}
    for row in existing:
        linked.setdefault(row.source_id, set()).add(row.target_id)
    counts = {
        key: 0
        for key in (
            "create",
            "existing",
            "missing_value",
            "unmatched",
            "ambiguous",
            "self_excluded",
            "cardinality_conflict",
        )
    }
    pairs, samples, issues = [], [], []
    issue_index = 0
    for source in sources:
        value = source.properties.get(rule.source_field)
        key = _value_key(value, family)
        matches = index.get(key, []) if key is not None else []
        target = matches[0] if len(matches) == 1 else None
        if key is None:
            status = "missing_value"
        elif not matches:
            status = "unmatched"
        elif len(matches) > 1:
            status = "ambiguous"
        elif source.id == target.id and not rule.allow_self:
            status = "self_excluded"
        elif target.id in linked.get(source.id, set()):
            status = "existing"
        elif relation["cardinality"] == "one" and linked.get(source.id):
            status = "cardinality_conflict"
        else:
            status = "create"
            pairs.append((source, target))
        counts[status] += 1
        sample = {
            "source_id": source.id,
            "source_revision": source.revision,
            "value": wire_value(value),
            "target_id": target.id if target else None,
            "target_revision": target.revision if target else None,
            "status": status,
            "candidate_count": len(matches),
        }
        if len(samples) < SAMPLE_SIZE:
            samples.append(sample)
        if status not in {"create", "existing"}:
            if issue_offset <= issue_index < issue_offset + issue_limit:
                issues.append(sample)
            issue_index += 1
    revision_state = {
        "rule": rule.model_dump(),
        "actor": ctx.user.id,
        "sources": [(x.id, x.revision, x.properties.get(rule.source_field)) for x in sources],
        "targets": [(x.id, x.revision, x.properties.get(rule.target_field)) for x in targets],
        "relations": [(x.id, x.revision, x.source_id, x.target_id) for x in existing],
    }
    report = {
        "plan_hash": fingerprint(revision_state),
        "counts": counts,
        "source_count": len(sources),
        "target_count": len(targets),
        "target_type": target_type["key"],
        "target_label": target_type["label"],
        "relation_label": relation["label"],
        "samples": samples,
        "issues": issues,
        "unresolved_count": sum(value for key, value in counts.items() if key not in {"create", "existing"}),
        "scope": "current_project_active_objects",
        "sample_limit": SAMPLE_SIZE,
        "semantics": "相同类型的精确值匹配；文本不自动去空格或改大小写，数值不经过浮点转换",
    }
    return report, pairs


@router.post("/relations/match-preview")
def preview(rule: MatchRule, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, rule.project_id, "objects:write")
        report, _ = matching_plan(session, ctx, rule)
        return report


@router.post("/relations/match-apply")
def apply_matches(data: ApplyMatch, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        ctx = access(session, user, data.rule.project_id, "objects:write")
        report, pairs = matching_plan(session, ctx, data.rule)
        if data.expected_plan_hash != report["plan_hash"]:
            conflict("对象、已有关系或规则已改变，请重新预览后确认")
        if report["unresolved_count"] and not data.allow_partial:
            raise DomainError(
                422,
                "unresolved_matches",
                "存在缺失、歧义或冲突；请修正，或明确选择只建立唯一匹配关系",
                report,
            )
        created = []
        for source, target in pairs:
            row = Relation(
                id=uid(),
                tenant_id=user.tenant_id,
                project_id=ctx.project.id,
                type_key=data.rule.relation_key,
                source_id=source.id,
                target_id=target.id,
                properties={},
                evidence=[],
            )
            session.add(row)
            created.append(
                {
                    "relation_id": row.id,
                    "source_id": source.id,
                    "source_revision": source.revision,
                    "target_id": target.id,
                    "target_revision": target.revision,
                }
            )
        session.flush()
        audit(
            session,
            user,
            ctx.project.id,
            "relations.matched",
            report["plan_hash"],
            {
                "rule": data.rule.model_dump(),
                "counts": report["counts"],
                "allow_partial": data.allow_partial,
                "created": created,
            },
        )
        return {
            "created": len(created),
            "existing": report["counts"]["existing"],
            "unresolved": report["unresolved_count"],
            "plan_hash": report["plan_hash"],
            "message": "关系已按本次确认的对象修订建立；后续字段变化不会自动重连或删除关系",
        }


@router.post("/relations/match-issues")
def match_issues(data: MatchIssuePage, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, data.rule.project_id, "objects:write")
        report, _ = matching_plan(session, ctx, data.rule, issue_offset=data.offset, issue_limit=data.limit)
        if data.expected_plan_hash != report["plan_hash"]:
            conflict("对象、已有关系或规则已改变，请重新预览后查看问题记录")
        return {
            "plan_hash": report["plan_hash"],
            "rows": report["issues"],
            "offset": data.offset,
            "limit": data.limit,
            "total": report["unresolved_count"],
        }
