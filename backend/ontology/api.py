from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ontology.core.config import Settings
from ontology.core.db import Database
from ontology.core.errors import DomainError
from ontology.identity.models import Tenant


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    database = Database(settings)
    if settings.environment != "production" and database.engine.dialect.name == "sqlite":
        database.create_schema()

    @asynccontextmanager
    async def lifespan(app):
        yield
        database.engine.dispose()

    app = FastAPI(title="企业本体工作台", version="0.1.0", lifespan=lifespan)
    app.state.db, app.state.settings = database, settings
    app.state.login_attempts = {}
    app.state.action_adapters = {}
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Requested-With"],
    )

    @app.middleware("http")
    async def browser_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if (
                origin
                and origin not in settings.allowed_origins
                and origin != str(request.base_url).rstrip("/")
            ):
                return JSONResponse({"error": {"code": "invalid_origin", "message": "请求来源不被允许"}}, 403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"error": {"code": "invalid_origin", "message": "拒绝跨站写入"}}, 403)
        result = await call_next(request)
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            result.headers["Cache-Control"] = "no-store"
        return result

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}, exc.status
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error(request, exc):
        return JSONResponse(
            {"error": {"code": "integrity_conflict", "message": "记录已存在或引用关系不合法"}}, 409
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path in {"/api/auth/login", "/api/users"}:
            return JSONResponse(
                {"error": {"code": "invalid_identity_request", "message": "账号字段格式不正确，请检查输入"}},
                422,
            )
        if request.url.path.startswith("/api/integrations/feishu/"):
            # FastAPI's default validation response includes raw input values,
            # which can contain an app secret or OAuth authorization code.
            return JSONResponse(
                {"error": {"code": "invalid_feishu_request", "message": "飞书请求字段不合法，请检查输入"}},
                422,
            )
        return await request_validation_exception_handler(request, exc)

    from ontology.connectors import ConnectorError

    @app.exception_handler(ConnectorError)
    async def connector_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, 422)

    @app.get("/api/health")
    def health():
        with database.transaction() as session:
            session.execute(select(1))
        return {"status": "ok", "version": "0.1.0", "storage": database.engine.dialect.name}

    @app.get("/api/setup/status")
    def setup_status():
        with database.transaction() as session:
            return {"initialized": bool(session.scalar(select(func.count()).select_from(Tenant)))}

    from ontology.identity.api import register_identity_routes

    register_identity_routes(app, database, settings)

    from ontology.actions.api import router as actions_router
    from ontology.cdc.api import router as cdc_router
    from ontology.cdc.projection_api import router as cdc_projection_router
    from ontology.documents.api import router as documents_router
    from ontology.documents.history_api import router as document_router
    from ontology.functions.api import router as functions_router
    from ontology.jobs.api import router as jobs_router
    from ontology.knowledge.api import router as knowledge_router
    from ontology.modeling.api import router as ontology_router
    from ontology.objects.api import router as objects_router
    from ontology.objects.contributions.api import router as contributions_router
    from ontology.objects.contributions.decision_api import router as decisions_router
    from ontology.search.content.api import router as content_search_router
    from ontology.search.objects.api import router as search_router
    from ontology.sources.api import router as sources_router
    from ontology.sources.mapping_preview import router as mapping_preview_router
    from ontology.sources.snapshot_api import router as ingestion_router

    app.include_router(ontology_router, prefix="/api")
    app.include_router(objects_router, prefix="/api")
    app.include_router(search_router, prefix="/api")
    app.include_router(contributions_router, prefix="/api")
    app.include_router(decisions_router, prefix="/api")
    app.include_router(documents_router, prefix="/api")
    app.include_router(knowledge_router, prefix="/api")
    app.include_router(jobs_router, prefix="/api")
    app.include_router(content_search_router, prefix="/api")
    app.include_router(document_router, prefix="/api")
    app.include_router(sources_router, prefix="/api")
    from ontology.integrations.feishu.api import router as feishu_router

    app.include_router(feishu_router, prefix="/api")
    app.include_router(ingestion_router, prefix="/api")
    app.include_router(mapping_preview_router, prefix="/api")
    app.include_router(actions_router, prefix="/api")
    app.include_router(functions_router, prefix="/api")
    app.include_router(cdc_projection_router, prefix="/api")
    app.include_router(cdc_router, prefix="/api")
    return app
