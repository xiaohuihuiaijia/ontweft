"""Goal-directed proposals are reviewed through existing model/import contracts."""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from ontology.content_access.models import DocumentSource
from ontology.core.domain import (
    active_definition,
    compare_and_swap,
    row_dict,
    scoped_record,
    validate_source_properties,
    wire_value,
)
from ontology.core.errors import DomainError, conflict
from ontology.core.schemas import RevisionInput
from ontology.documents.history import capture_current_parse
from ontology.documents.storage_models import Document
from ontology.identity.models import Project
from ontology.identity.security import access, audit, authenticated
from ontology.intelligence.models import ModelConnection
from ontology.intelligence.proposal_models import AIPage, AIProposal
from ontology.intelligence.proposal_schemas import ProposalInput, ProposalUpdate
from ontology.jobs.models import Job
from ontology.modeling.models import ChangeSet, Release
from ontology.modeling.schemas import Definition
from ontology.sources.models import Source

router = APIRouter()
JOB_PROGRESS_FIELDS = (
    "stage", "pages_total", "pages_completed", "current_page", "model_batches_completed",
    "model_batches_total", "facts_pages_completed", "cost_cny", "reserved_cny", "requests",
    "last_model", "models_used", "usage",
)


def check_inputs(session, proposal, user, *, current_versions=True):
    ctx = access(session, user, proposal.project_id, "ontology:write")
    access(session, user, proposal.project_id, "sources:write")
    for item in proposal.inputs.get("documents", []):
        doc = scoped_record(session, Document, item["id"], user)
        if doc.project_id != proposal.project_id:
            raise DomainError(403, "ai_source_scope", "来源文档不属于当前项目")
        if current_versions and (doc.revision != item["revision"] or doc.content_hash != item["hash"]):
            conflict("来源文档版本已改变，请基于新版本重新生成")
        if session.scalar(select(DocumentSource.id).where(DocumentSource.document_id == doc.id)):
            raise DomainError(
                422, "ai_source_not_supported", "首期仅支持本地上传文档，外部授权资料不自动发送到模型"
            )
    for item in proposal.inputs.get("sources", []):
        source = scoped_record(session, Source, item["id"], user)
        if source.project_id != proposal.project_id:
            raise DomainError(403, "ai_source_scope", "数据来源不属于当前项目")
        if current_versions and source.revision != item["revision"]:
            conflict("数据来源配置已改变，请基于新版本重新生成")
        if source.kind not in {"postgres", "postgresql", "tabular"}:
            raise DomainError(422, "ai_source_not_supported", "此来源尚未启用 AI 建模")
    return ctx


def review_definition(session, proposal, change=None):
    if not proposal.changeset_id:
        return proposal.definition
    change = change or session.get(ChangeSet, proposal.changeset_id)
    if change is None:
        conflict("绑定的模型变更集不可用")
    if change.status == "published":
        release = session.get(Release, change.release_id)
        if release is None:
            conflict("绑定的模型发布版本不可用")
        return release.definition
    return change.definition


def view(session, proposal):
    result = row_dict(proposal)
    result["inputs"] = {key: value for key, value in proposal.inputs.items() if key != "document_resources"}
    job = session.get(Job, proposal.job_id) if proposal.job_id else None
    if job:
        result["job"] = {k: getattr(job, k) for k in ("id", "status", "error")}
        # The worker checkpoint also holds database samples and intermediate models.
        # The UI only needs progress and accounting, not another unpaged result copy.
        result["job"]["checkpoint"] = {
            k: v
            for k, v in job.checkpoint.items()
            if k in JOB_PROGRESS_FIELDS
        }
        if proposal.status in {"pending", "running"}:
            result["status"] = job.status if job.status != "succeeded" else proposal.status
    result["candidate_count"] = len(proposal.candidates)
    from ontology.intelligence.projection import import_preconditions

    change = session.get(ChangeSet, proposal.changeset_id) if proposal.changeset_id else None
    result["review_definition"] = review_definition(session, proposal, change)
    from ontology.intelligence.model_notes import notes_view
    from ontology.modeling.diff import definition_diff

    result["model_notes"] = notes_view(proposal.model_notes or [], result["review_definition"])
    baseline = session.get(Release, proposal.baseline_release_id) if proposal.baseline_release_id else None
    result["model_diff"] = definition_diff(baseline.definition if baseline else {"types": [], "mappings": []}, result["review_definition"])
    result["review_changeset_revision"] = change.revision if change else None
    try:
        import_preconditions(session, proposal, session.get(Project, proposal.project_id))
    except DomainError as exc:
        result.update(import_ready=False, import_block_reason=exc.message)
    else:
        result.update(import_ready=True, import_block_reason=None)
    return result


def summary_jobs(session, job_ids, tenant_id, project_id):
    if not job_ids:
        return {}
    columns = [Job.id, Job.status, Job.error]
    for key in JOB_PROGRESS_FIELDS:
        # Preserve missing versus explicit JSON null without loading the private
        # checkpoint (which also contains model output and database samples).
        kind = (func.json_type(Job.checkpoint, f"$.{key}")
                if session.get_bind().dialect.name == "sqlite"
                else func.json_typeof(Job.checkpoint[key]))
        columns.extend((Job.checkpoint[key].label(key), kind.label(f"kind_{key}")))
    rows = session.execute(select(*columns).where(
        Job.tenant_id == tenant_id, Job.project_id == project_id, Job.id.in_(job_ids),
    )).mappings()
    return {
        row["id"]: {
            "id": row["id"], "status": row["status"], "error": row["error"],
            "checkpoint": {key: row[key] for key in JOB_PROGRESS_FIELDS if row[f"kind_{key}"] is not None},
        }
        for row in rows
    }


@router.post("/ai/proposals", status_code=202)
def create_proposal(data: ProposalInput, request: Request, user=Depends(authenticated)):
    if not data.document_ids and not data.source_selections:
        raise DomainError(422, "empty_ai_selection", "请选择至少一份文档或数据资源")
    if set(data.pages) - set(data.document_ids) or any(
        not numbers or any(number < 1 for number in numbers) for numbers in data.pages.values()
    ):
        raise DomainError(422, "invalid_page_selection", "页码范围须属于所选文档且非空，页码从 1 开始")
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        ctx = access(session, user, data.project_id, "ontology:write")
        connection = scoped_record(session, ModelConnection, data.connection_id, user)
        if not connection.enabled:
            conflict("模型连接已停用")
        inputs = {
            "documents": [],
            "sources": [],
            "instructions": data.instructions,
            "quality": data.quality,
            "budget_cny": data.budget_cny,
            "pages": {key: sorted(set(numbers)) for key, numbers in data.pages.items()},
        }
        for doc_id in dict.fromkeys(data.document_ids):
            doc = scoped_record(session, Document, doc_id, user)
            selected = {"id": doc.id, "revision": doc.revision, "hash": doc.content_hash}
            if not doc.filename.lower().endswith((".pdf", ".png", ".jpg", ".jpeg")):
                # Logical Office/text pages depend on interpretation as well as
                # original bytes. None explicitly freezes the native fallback.
                snapshot = capture_current_parse(session, doc)
                selected["native_parse_id"] = snapshot.id if snapshot else None
            inputs["documents"].append(selected)
        for selection in data.source_selections:
            source = scoped_record(session, Source, selection.source_id, user)
            resources = {r["resource_id"]: r for r in source.resources}
            if any(r not in resources for r in selection.resource_ids):
                raise DomainError(422, "invalid_resources", "请选择已发现的数据资源")
            inputs["sources"].append(
                {
                    "id": source.id,
                    "revision": source.revision,
                    "resources": [resources[r] for r in dict.fromkeys(selection.resource_ids)],
                }
            )
        proposal = AIProposal(
            tenant_id=user.tenant_id,
            project_id=data.project_id,
            owner_id=user.id,
            connection_id=connection.id,
            connection_revision=connection.revision,
            baseline_release_id=ctx.project.active_release_id,
            inputs=inputs,
            definition=active_definition(session, ctx.project),
        )
        check_inputs(session, proposal, user)
        session.add(proposal)
        session.flush()
        job = Job(
            tenant_id=user.tenant_id,
            project_id=data.project_id,
            owner_id=user.id,
            kind="ai.propose",
            payload={"proposal_id": proposal.id},
        )
        session.add(job)
        session.flush()
        proposal.job_id = job.id
        audit(session, user, data.project_id, "ai.proposal.created", proposal.id)
        return view(session, proposal)


@router.get("/ai/proposals")
def proposals(
    request: Request,
    project_id: str,
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        access(session, user, project_id, "ontology:write")
        access(session, user, project_id, "sources:write")
        conditions = (AIProposal.tenant_id == user.tenant_id, AIProposal.project_id == project_id)
        columns = [column for column in AIProposal.__table__.columns
                   if column.name not in {"candidates", "relations", "definition", "model_notes"}]
        items = [dict(row) for row in session.execute(
            select(*columns, func.json_array_length(AIProposal.candidates).label("candidate_count"))
            .where(*conditions)
            .order_by(AIProposal.created_at.desc(), AIProposal.id.desc())
            .offset(offset)
            .limit(limit)
        ).mappings()]
        jobs = summary_jobs(session, {item["job_id"] for item in items if item["job_id"]}, user.tenant_id, project_id)
        for item in items:
            item["inputs"] = {key: value for key, value in item["inputs"].items() if key != "document_resources"}
            job = jobs.get(item["job_id"])
            if job:
                item["job"] = job
                if item["status"] in {"pending", "running"} and job["status"] != "succeeded":
                    item["status"] = job["status"]
        return {
            "items": items,
            "total": session.scalar(select(func.count()).select_from(AIProposal).where(*conditions)),
            "limit": limit,
            "offset": offset,
        }


@router.get("/ai/proposals/{proposal_id}")
def proposal_detail(proposal_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        proposal = scoped_record(session, AIProposal, proposal_id, user)
        check_inputs(session, proposal, user, current_versions=False)
        return view(session, proposal)


@router.get("/ai/proposals/{proposal_id}/pages")
def interpreted_pages(
    proposal_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        proposal = scoped_record(session, AIProposal, proposal_id, user)
        check_inputs(session, proposal, user, current_versions=False)
        job = session.get(Job, proposal.job_id)
        ids = list(job.checkpoint.get("pages", {}).values())
        selected = ids[offset : offset + limit]
        rows = {
            row.id: row
            for row in session.scalars(
                select(AIPage).where(
                    AIPage.id.in_(selected),
                    AIPage.tenant_id == user.tenant_id,
                    AIPage.project_id == proposal.project_id,
                )
            )
        }
        return {
            "items": [row_dict(rows[id]) for id in selected if id in rows],
            "total": len(ids),
            "limit": limit,
            "offset": offset,
        }


def check_evidence(session, proposal, items):
    allowed = {d["id"] for d in proposal.inputs["documents"]}
    job = session.get(Job, proposal.job_id)
    pages = {
        row.id: row
        for row in session.scalars(
            select(AIPage).where(
                AIPage.id.in_(list(job.checkpoint.get("pages", {}).values())),
                AIPage.tenant_id == proposal.tenant_id,
            )
        )
    }
    page_text = {id: "".join(page.result.get("text", "").split()) for id, page in pages.items()}
    for item in items:
        for evidence in item.get("evidence", []):
            page = pages.get(evidence.get("parse_id"))
            if (
                not page
                or evidence["document_id"] not in allowed
                or page.document_id != evidence["document_id"]
                or page.page_number != evidence["page"]
                or page.result.get("native_parse_id") != evidence.get("native_parse_id")
            ):
                raise DomainError(422, "invalid_ai_evidence", "候选证据必须来自本次已处理的原始页面")
            quote = "".join(evidence["quote"].split())
            if not quote or quote not in page_text[page.id]:
                raise DomainError(422, "invalid_ai_quote", "候选引文不在对应解析文本中")


def check_candidates(definition, candidates, relations):
    """Review edits share the existing property rules; identity must match the facts."""
    types = {item["key"]: item for item in definition["types"]}
    by_id = {}
    for candidate in candidates:
        if candidate["id"] in by_id:
            raise DomainError(422, "invalid_ai_candidate", "候选标识须唯一")
        by_id[candidate["id"]] = candidate
        # Excluded records remain evidence history even if their suggested type is removed.
        if not candidate.get("selected", True):
            continue
        type_def = types.get(candidate["type_key"])
        if not type_def:
            raise DomainError(422, "invalid_ai_candidate", "候选标识须唯一且类型须存在于本体草稿")
        candidate["properties"] = wire_value(validate_source_properties(type_def, candidate["properties"]))
        key = candidate.get("business_key") or {}
        if any(
            field not in candidate["properties"]
            or value in (None, "")
            or candidate["properties"][field] != value
            for field, value in key.items()
        ) or not key:
            raise DomainError(
                422, "invalid_candidate_identity", "业务键须与候选属性一致；选中实例须有完整业务键"
            )
    seen = set()
    for relation in relations:
        if relation["id"] in seen:
            raise DomainError(422, "invalid_ai_relation", "关系候选标识须唯一")
        seen.add(relation["id"])
        if not relation.get("selected", True):
            continue
        source, target = (
            by_id.get(relation["source_candidate_id"]),
            by_id.get(relation["target_candidate_id"]),
        )
        declared = (
            next(
                (
                    item
                    for item in types.get(source["type_key"], {}).get("relations", [])
                    if item["key"] == relation["key"]
                ),
                None,
            )
            if source
            else None
        )
        if (
            not target
            or not declared
            or declared["target_type"] != target["type_key"]
            or (
                relation.get("selected", True)
                and not (source.get("selected", True) and target.get("selected", True))
            )
        ):
            raise DomainError(
                422,
                "invalid_ai_relation",
                "关系须使用已声明的关系类型及匹配的候选端点，选中关系须同时选中端点",
            )
        relation["properties"] = wire_value(validate_source_properties(declared, relation.get("properties", {})))


@router.patch("/ai/proposals/{proposal_id}")
def edit_proposal(proposal_id: str, data: ProposalUpdate, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        proposal = scoped_record(session, AIProposal, proposal_id, user)
        ctx = check_inputs(session, proposal, user)
        if proposal.imported or proposal.status not in {"ready", "adopted"}:
            conflict("仅可编辑尚未导入的候选")
        if proposal.status == "adopted" and data.definition is not None:
            conflict("已采纳模型请在现有变更集中编辑")
        if proposal.status == "adopted" and not proposal.changeset_id and (
            ctx.project.active_release_id != proposal.baseline_release_id
        ):
            conflict("本体发布基线已改变，请基于当前发布版重新核对候选")
        change = None
        if proposal.changeset_id:
            change = scoped_record(session, ChangeSet, proposal.changeset_id, user)
            session.refresh(change, with_for_update=True)
            if data.expected_changeset_revision != change.revision:
                conflict("模型变更集修订已变化，请刷新候选并重新核对")
        values = {
            k: v for k, v in data.model_dump(exclude_none=True).items()
            if k not in {"expected_revision", "expected_changeset_revision"}
        }
        check_evidence(session, proposal, values.get("candidates", []) + values.get("relations", []))
        check_candidates(
            values.get("definition", review_definition(session, proposal, change)),
            values.get("candidates", proposal.candidates),
            values.get("relations", proposal.relations),
        )
        compare_and_swap(session, proposal, data.expected_revision, values)
        if change is not None:
            from ontology.intelligence.projection import refresh_adopted_mapping

            refresh_adopted_mapping(session, proposal, change, user)
        elif proposal.status == "adopted":
            from ontology.intelligence.projection import prepare_document_source

            prepare_document_source(session, proposal, user)
            definition = Definition.model_validate(proposal.definition).model_dump()
            if definition != Definition.model_validate(active_definition(session, ctx.project)).model_dump():
                change = ChangeSet(
                    tenant_id=user.tenant_id, project_id=proposal.project_id, author_id=user.id,
                    title="AI 本体草稿", definition=definition,
                    baseline_release_id=proposal.baseline_release_id,
                )
                session.add(change)
                session.flush()
                proposal.changeset_id = change.id
        audit(session, user, proposal.project_id, "ai.proposal.edited", proposal.id, {
            "revision": proposal.revision,
            "candidate_count": len(proposal.candidates),
            "relation_count": len(proposal.relations),
        })
        return view(session, proposal)


@router.post("/ai/proposals/{proposal_id}/adopt")
def adopt(proposal_id: str, data: RevisionInput, request: Request, user=Depends(authenticated)):
    from ontology.intelligence.projection import prepare_document_source

    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        proposal = scoped_record(session, AIProposal, proposal_id, user)
        ctx = check_inputs(session, proposal, user)
        if proposal.changeset_id:
            return {
                "proposal": view(session, proposal),
                "changeset": row_dict(session.get(ChangeSet, proposal.changeset_id)),
            }
        if proposal.status != "ready" or proposal.revision != data.expected_revision:
            conflict("候选版本已改变")
        if ctx.project.active_release_id != proposal.baseline_release_id:
            conflict("本体发布基线已改变，请基于最新模型重新生成")
        check_evidence(session, proposal, proposal.candidates + proposal.relations)
        check_candidates(proposal.definition, proposal.candidates, proposal.relations)
        prepare_document_source(session, proposal, user)
        definition = Definition.model_validate(proposal.definition).model_dump()
        if definition == Definition.model_validate(active_definition(session, ctx.project)).model_dump():
            proposal.status = "adopted"
            proposal.revision += 1
            return {"proposal": view(session, proposal), "changeset": None}
        change = ChangeSet(
            tenant_id=user.tenant_id,
            project_id=proposal.project_id,
            author_id=user.id,
            title="AI 本体草稿",
            definition=definition,
            baseline_release_id=proposal.baseline_release_id,
        )
        session.add(change)
        session.flush()
        proposal.changeset_id, proposal.status = change.id, "adopted"
        proposal.revision += 1
        audit(session, user, proposal.project_id, "ai.proposal.adopted", proposal.id)
        return {"proposal": view(session, proposal), "changeset": row_dict(change)}


@router.post("/ai/proposals/{proposal_id}/import")
def import_candidates(proposal_id: str, data: RevisionInput, request: Request, user=Depends(authenticated)):
    from ontology.intelligence.projection import import_proposal

    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        proposal = scoped_record(session, AIProposal, proposal_id, user)
        ctx = check_inputs(session, proposal, user)
        if not proposal.imported and proposal.revision != data.expected_revision:
            conflict()
        result = import_proposal(session, proposal, ctx)
        return {**result, "proposal": view(session, proposal)}
