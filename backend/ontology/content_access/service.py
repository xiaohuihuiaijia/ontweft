"""Current source permissions, applied before content counts and pagination.

Remote checks run before the content transaction. Observations have bounded
freshness, fenced refresh reservations and no credential/body fields. Public
project membership alone never grants an imported document's source rights.
"""

import asyncio
import time

from fastapi import Depends, Request
from sqlalchemy import and_, select

from ontology.content_access.models import DocumentSource, KnowledgeDependency, SourcePermission
from ontology.core.errors import DomainError, missing
from ontology.documents.storage_models import Document
from ontology.identity.models import LoginSession, Project
from ontology.identity.security import access, authenticated, token_hash
from ontology.integrations.feishu.gateway import FeishuGatewayError, FeishuUserGateway
from ontology.integrations.feishu.models import FeishuAccount, FeishuApplication
from ontology.integrations.feishu.oauth import READ_SCOPES
from ontology.integrations.feishu.selection import _gather
from ontology.integrations.feishu.service import EmployeeService
from ontology.jobs.models import Job
from ontology.knowledge.models import KnowledgePage
from ontology.search.objects.api import read_context

FRESH_SECONDS = 30
REFRESH_SECONDS = 45
WHOLE_DOCUMENT_TYPES = ("docx", "file")


def source_unknown():
    raise DomainError(
        503, "source_authorization_unknown", "来源权限暂时无法确认，请稍后重试或检查个人飞书授权"
    )


def _observations(ctx, action="view"):
    source, grant, account, app = DocumentSource, SourcePermission, FeishuAccount, FeishuApplication
    query = (
        select(source.document_id, grant.status)
        .join(grant, and_(grant.source_id == source.id, grant.user_id == ctx.user.id, grant.action == action))
        .join(app, app.id == source.application_id)
        .join(account, and_(account.application_id == app.id, account.user_id == ctx.user.id))
        .where(
            source.tenant_id == ctx.user.tenant_id,
            source.project_id == ctx.project.id,
            source.resource_type.in_(WHOLE_DOCUMENT_TYPES),
            grant.tenant_id == ctx.user.tenant_id,
            account.tenant_id == ctx.user.tenant_id,
            app.tenant_id == ctx.user.tenant_id,
            app.enabled.is_(True),
            app.service == source.service,
            app.external_tenant_key == source.external_tenant_key,
            account.status == "active",
            account.expires_at > time.time(),
            account.user_revision == ctx.user.revision,
            account.application_revision == app.revision,
            grant.account_revision == account.revision,
            grant.user_revision == ctx.user.revision,
            grant.application_revision == app.revision,
            grant.valid_until > time.time(),
        )
    )
    if hasattr(ctx, "content_application_ids"):
        query = query.where(source.application_id.in_(ctx.content_application_ids))
    return query.correlate(None)


def document_condition(ctx, document_id=Document.id, *, action="view"):
    allowed = (
        _observations(ctx, action)
        .with_only_columns(DocumentSource.document_id)
        .where(SourcePermission.status == "allowed")
    )
    denied = select(DocumentSource.document_id).where(
        DocumentSource.tenant_id == ctx.user.tenant_id,
        DocumentSource.project_id == ctx.project.id,
        DocumentSource.document_id.not_in(allowed),
    )
    result = document_id.not_in(denied)
    return and_(result, document_condition(ctx, document_id)) if action != "view" else result


def knowledge_condition(ctx, page_id=KnowledgePage.id, revision=None):
    revision = KnowledgePage.revision if revision is None else revision
    blocked = select(KnowledgeDependency.page_id).where(
        KnowledgeDependency.tenant_id == ctx.user.tenant_id,
        KnowledgeDependency.project_id == ctx.project.id,
        KnowledgeDependency.revision == revision,
        ~document_condition(ctx, KnowledgeDependency.document_id),
    )
    return page_id.not_in(blocked)


def job_condition(ctx):
    return (Job.kind != "document.parse") | document_condition(ctx, Job.payload["document_id"].as_string())


def audit_condition(ctx):
    from ontology.audit.models import Audit

    return and_(
        ~Audit.event.startswith("document.") | document_condition(ctx, Audit.resource_id),
        ~Audit.event.startswith("knowledge.")
        | Audit.resource_id.in_(
            select(KnowledgePage.id).where(
                KnowledgePage.tenant_id == ctx.user.tenant_id,
                KnowledgePage.project_id == ctx.project.id,
                knowledge_condition(ctx),
            )
        ),
        ~Audit.event.startswith("job.")
        | Audit.resource_id.in_(
            select(Job.id).where(
                Job.tenant_id == ctx.user.tenant_id, Job.project_id == ctx.project.id, job_condition(ctx)
            )
        ),
    )


def require_job_source(session, job, user, member):
    from ontology.identity.security import ROLES, Access

    source = session.scalar(
        select(DocumentSource).where(
            DocumentSource.tenant_id == job.tenant_id,
            DocumentSource.project_id == job.project_id,
            DocumentSource.document_id == job.payload.get("document_id"),
        )
    )
    if source is None:
        return
    session.scalar(
        select(FeishuApplication)
        .where(FeishuApplication.id == source.application_id)
        .with_for_update(read=True)
    )
    account = session.scalar(
        select(FeishuAccount)
        .where(FeishuAccount.application_id == source.application_id, FeishuAccount.user_id == user.id)
        .with_for_update(read=True)
    )
    ctx = Access(user, session.get(Project, job.project_id), member.role, ROLES[member.role])
    ctx.content_application_ids = (
        {account.application_id} if account and set(READ_SCOPES).issubset(account.scopes) else set()
    )
    if not session.scalar(
        select(Document.id).where(Document.id == source.document_id, document_condition(ctx))
    ):
        raise DomainError(
            403, "job_source_authorization_required", "任务的来源授权已失效或需要重新核验；未提交解析结果"
        )


def require_document(session, ctx, document_id, *, action="view"):
    if not session.scalar(
        select(Document.id).where(
            Document.id == document_id,
            Document.tenant_id == ctx.user.tenant_id,
            Document.project_id == ctx.project.id,
            document_condition(ctx, action=action),
        )
    ):
        missing()


def require_knowledge(session, ctx, page, *, revision=None):
    revision = page.revision if revision is None else revision
    if not session.scalar(
        select(KnowledgePage.id).where(
            KnowledgePage.id == page.id,
            KnowledgePage.tenant_id == ctx.user.tenant_id,
            KnowledgePage.project_id == ctx.project.id,
            knowledge_condition(ctx, revision=revision),
        )
    ):
        missing()


def inherit_dependencies(session, page):
    # Removing a display citation does not prove that private derived text was
    # removed. Dependencies only grow; a future reviewed declassification must
    # be a separate operation, never an implicit effect of editing references.
    previous = session.scalars(
        select(KnowledgeDependency.document_id).where(
            KnowledgeDependency.tenant_id == page.tenant_id,
            KnowledgeDependency.project_id == page.project_id,
            KnowledgeDependency.page_id == page.id,
            KnowledgeDependency.revision == page.revision - 1,
        )
    ).all()
    document_ids = set(previous) | {ref["document_id"] for ref in page.references}
    for document_id in sorted(document_ids):
        session.add(
            KnowledgeDependency(
                tenant_id=page.tenant_id,
                project_id=page.project_id,
                page_id=page.id,
                revision=page.revision,
                document_id=document_id,
            )
        )


def content_context(session, request, user, project_id, *, permission="objects:read"):
    # Hold a project read lock while serializing content; permission observation
    # commits take the same project's write lock. Lock app/account rows in the
    # same order as disconnect/configuration so another project cannot revoke a
    # binding between the final check and reading protected bytes.
    if permission != "objects:read":
        access(session, user, project_id, permission)
    ctx = read_context(session, user, project_id)
    login = session.scalar(
        select(LoginSession)
        .where(LoginSession.token_hash == token_hash(request.cookies.get("ontology_session", "")))
        .with_for_update(read=True)
    )
    if (
        not login
        or login.user_id != user.id
        or login.user_revision != user.revision
        or login.expires_at <= time.time()
    ):
        raise DomainError(401, "unauthenticated", "会话已失效，请重新登录")
    application_ids = select(DocumentSource.application_id).where(
        DocumentSource.tenant_id == user.tenant_id, DocumentSource.project_id == project_id
    )
    session.scalars(
        select(FeishuApplication)
        .where(FeishuApplication.id.in_(application_ids))
        .order_by(FeishuApplication.id)
        .with_for_update(read=True)
    ).all()
    accounts = session.scalars(
        select(FeishuAccount)
        .where(FeishuAccount.application_id.in_(application_ids), FeishuAccount.user_id == user.id)
        .order_by(FeishuAccount.application_id)
        .with_for_update(read=True)
    ).all()
    ctx.content_application_ids = {
        row.application_id for row in accounts if set(READ_SCOPES).issubset(row.scopes)
    }
    bound_sources = (
        select(DocumentSource.id)
        .join(FeishuApplication, FeishuApplication.id == DocumentSource.application_id)
        .join(
            FeishuAccount,
            and_(FeishuAccount.application_id == FeishuApplication.id, FeishuAccount.user_id == user.id),
        )
        .where(
            DocumentSource.tenant_id == user.tenant_id,
            DocumentSource.project_id == project_id,
            DocumentSource.application_id.in_(ctx.content_application_ids),
            FeishuApplication.enabled.is_(True),
            FeishuApplication.service == DocumentSource.service,
            FeishuApplication.external_tenant_key == DocumentSource.external_tenant_key,
            FeishuAccount.status == "active",
            FeishuAccount.expires_at > time.time(),
            FeishuAccount.user_revision == user.revision,
            FeishuAccount.application_revision == FeishuApplication.revision,
        )
    )
    for action in ("view", "export") if request.url.path.endswith("/download") else ("view",):
        known = (
            _observations(ctx, action)
            .with_only_columns(DocumentSource.id)
            .where(SourcePermission.status.in_(("allowed", "denied")))
        )
        if session.scalar(bound_sources.where(DocumentSource.id.not_in(known)).limit(1)):
            source_unknown()
    return ctx


def invalidate_observed_resource(employee, application_id, kind, token, *, denied=False):
    """A newer negative preview fences cached or in-flight positive checks."""
    from sqlalchemy import update

    with employee.db.transaction(employee.user.tenant_id) as session:
        sources = session.scalars(
            select(DocumentSource).where(
                DocumentSource.tenant_id == employee.user.tenant_id,
                DocumentSource.application_id == application_id,
                DocumentSource.resource_type == kind,
                DocumentSource.resource_token == token,
            )
        ).all()
        for project_id in sorted({row.project_id for row in sources}):
            session.scalar(
                select(Project)
                .where(Project.id == project_id, Project.tenant_id == employee.user.tenant_id)
                .with_for_update()
            )
        session.execute(
            update(SourcePermission)
            .where(
                SourcePermission.tenant_id == employee.user.tenant_id,
                SourcePermission.source_id.in_([row.id for row in sources]),
                SourcePermission.user_id == employee.user.id,
            )
            .values(
                status="denied" if denied else "unknown",
                revision=SourcePermission.revision + 1,
                checked_at=time.time(),
                lease_until=0,
                valid_until=time.time() + FRESH_SECONDS if denied else 0,
            )
        )


async def refresh_project(request, user, project_id, *, actions=("view",)):
    employee = EmployeeService(request, user, permission="objects:read")
    with employee.db.transaction(user.tenant_id) as session:
        employee._guard(session, project_id)
        sources = session.scalars(
            select(DocumentSource).where(
                DocumentSource.tenant_id == user.tenant_id, DocumentSource.project_id == project_id
            )
        ).all()
    if not sources:
        return
    contexts = {}
    for application_id in sorted({source.application_id for source in sources}):
        try:
            contexts[application_id] = await employee.context(project_id, application_id)
        except DomainError as exc:
            if exc.status == 401:
                raise
            if exc.code == "feishu_refresh_in_progress":
                source_unknown()
            if exc.status not in {404, 409}:
                source_unknown()
            # No current personal binding: platform membership does not grant
            # access. The SQL predicate also checks current binding status.
    slots = asyncio.Semaphore(4)

    async def check(source, action):
        context_and_region = contexts.get(source.application_id)
        if not context_and_region:
            return
        context, region = context_and_region
        if source.resource_type not in WHOLE_DOCUMENT_TYPES:
            source_unknown()  # Table/record/field visibility requires its own proven scope.
        if region != source.service or context.external_tenant_key != source.external_tenant_key:
            return
        async with slots:
            with employee.db.transaction(user.tenant_id) as session:
                app, account = employee._current(
                    session, project_id, source.application_id, context.credential_revision
                )
                row = session.scalar(
                    select(SourcePermission)
                    .where(
                        SourcePermission.source_id == source.id,
                        SourcePermission.user_id == user.id,
                        SourcePermission.action == action,
                    )
                    .with_for_update()
                )
                same_binding = row and (
                    row.account_revision,
                    row.application_revision,
                    row.user_revision,
                ) == (account.revision, app.revision, user.revision)
                if same_binding and row.status in {"allowed", "denied"} and row.valid_until > time.time():
                    return
                if same_binding and row.status == "checking" and row.lease_until > time.time():
                    source_unknown()
                if row is None:
                    row = SourcePermission(
                        tenant_id=user.tenant_id,
                        project_id=project_id,
                        source_id=source.id,
                        user_id=user.id,
                        action=action,
                        revision=0,
                    )
                    session.add(row)
                row.revision += 1
                row.account_revision, row.application_revision, row.user_revision = (
                    account.revision,
                    app.revision,
                    user.revision,
                )
                row.status, row.valid_until, row.lease_until = "checking", 0, time.time() + REFRESH_SECONDS
                session.flush()
                reservation_id, reservation_revision = row.id, row.revision
            outcome = "unknown"
            try:
                answer = await FeishuUserGateway(context, service=region).check_permission(
                    source.resource_type, source.resource_token, action=action
                )
                outcome = "allowed" if answer.allowed else "denied"
            except FeishuGatewayError as exc:
                if exc.code == "permission_denied":
                    outcome = "denied"
                elif exc.code in {"credential_invalid", "credential_expired"}:
                    employee._fail(source.application_id, context.credential_revision)
            finally:
                with employee.db.transaction(user.tenant_id) as session:
                    # A cancelled/late worker only changes its own reservation.
                    session.scalar(
                        select(Project)
                        .where(Project.id == project_id, Project.tenant_id == user.tenant_id)
                        .with_for_update()
                    )
                    row = session.get(SourcePermission, reservation_id)
                    if row and row.revision == reservation_revision and row.status == "checking":
                        row.status, row.checked_at, row.lease_until = outcome, time.time(), 0
                        row.valid_until = time.time() + (FRESH_SECONDS if outcome != "unknown" else 0)
            if outcome == "unknown":
                source_unknown()

    try:
        async with asyncio.timeout(REFRESH_SECONDS):
            await _gather(check(source, action) for source in sources for action in actions)
    except TimeoutError:
        source_unknown()


async def prepare_content_access(request: Request, user=Depends(authenticated)):
    # Route dependencies run before sync content handlers open transactions.
    path = request.url.path
    if path.endswith("/documents/upload"):
        return
    project_id = request.query_params.get("project_id")
    if not project_id and request.method in {"POST", "PATCH"} and not request.path_params:
        try:
            body = await request.json()
        except ValueError:
            return  # The route's normal body validation reports malformed JSON.
        project_id = body.get("project_id") if isinstance(body, dict) else None
    if not project_id:
        mapping = (("document_id", Document), ("page_id", KnowledgePage), ("job_id", Job))
        with request.app.state.db.transaction(user.tenant_id) as session:
            for key, model in mapping:
                if key in request.path_params:
                    row = session.scalar(
                        select(model).where(
                            model.id == request.path_params[key], model.tenant_id == user.tenant_id
                        )
                    )
                    if row is None:
                        missing()
                    access(session, user, row.project_id)
                    project_id = row.project_id
                    break
    if isinstance(project_id, str) and project_id:
        await refresh_project(
            request, user, project_id, actions=("view", "export") if path.endswith("/download") else ("view",)
        )
