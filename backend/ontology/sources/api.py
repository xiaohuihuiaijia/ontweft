import json
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy import select

from ontology.core.domain import exact_json_text, scoped_record
from ontology.core.errors import DomainError
from ontology.core.schemas import StrictModel
from ontology.identity.security import access, audit, authenticated
from ontology.sources.models import Source
from ontology.sources.service import (
    cipher,
    connector_for,
    require_source_read_identity,
    safe_config,
    source_view,
)

router = APIRouter()


class SourceInput(StrictModel):
    project_id: str
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["sqlite", "postgres", "mysql", "file", "rdf", "http", "mongo", "neo4j", "feishu"]
    config: dict
    credentials: dict[str, str] = Field(default_factory=dict)


@router.get("/sources")
def sources(request: Request, project_id: str, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        access(session, user, project_id, "sources:write")
        rows = session.scalars(
            select(Source)
            .where(Source.tenant_id == user.tenant_id, Source.project_id == project_id)
            .order_by(Source.created_at.desc())
        ).all()
        return {"items": [source_view(x) for x in rows], "total": len(rows)}


@router.post("/sources", status_code=201)
def create_source(data: SourceInput, request: Request, user=Depends(authenticated)):
    safe_config(data.config)
    if set(data.credentials) - {"password", "token"}:
        raise DomainError(422, "unsupported_credential", "未知凭据类型")
    with request.app.state.db.transaction(user.tenant_id) as session:
        access(session, user, data.project_id, "sources:write")
        secret = (
            cipher(request.app.state.settings).encrypt(json.dumps(data.credentials).encode()).decode()
            if data.credentials
            else ""
        )
        source = Source(
            tenant_id=user.tenant_id,
            project_id=data.project_id,
            name=data.name,
            kind=data.kind,
            config=data.config,
            secret=secret,
        )
        connector = connector_for(source, request.app.state.settings)
        connector.close()
        session.add(source)
        session.flush()
        audit(session, user, data.project_id, "source.created", source.id)
        return source_view(source)


@router.post("/sources/{source_id}/test")
def test_source(source_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        source = scoped_record(session, Source, source_id, user)
        access(session, user, source.project_id, "sources:write")
        require_source_read_identity(source)
        connector = connector_for(source, request.app.state.settings)
        try:
            result = connector.test_connection()
            source.status = "connected" if result.ok else "error"
            source.last_error = None if result.ok else result.message
            return result.model_dump()
        finally:
            connector.close()


@router.post("/sources/{source_id}/discover")
def discover_source(source_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        source = scoped_record(session, Source, source_id, user)
        access(session, user, source.project_id, "sources:write")
        require_source_read_identity(source)
        connector = connector_for(source, request.app.state.settings)
        try:
            resources = [r.model_dump(mode="json") for r in connector.discover()]
            from ontology.sources.ingestion import schema_fingerprint

            for resource in resources:
                resource["schema_fingerprint"] = schema_fingerprint(resource)
            source.resources, source.status, source.last_error = resources, "discovered", None
            audit(
                session,
                user,
                source.project_id,
                "source.discovered",
                source.id,
                {"resources": len(resources)},
            )
            return {
                "source": source_view(source),
                "resources": resources,
                "capabilities": connector.capabilities.model_dump(),
            }
        finally:
            connector.close()


@router.get("/sources/{source_id}/sample")
def sample_source(
    source_id: str,
    request: Request,
    resource: str,
    limit: int = Query(20, ge=1, le=100),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        source = scoped_record(session, Source, source_id, user)
        access(session, user, source.project_id, "sources:write")
        require_source_read_identity(source)
        connector = connector_for(source, request.app.state.settings)
        try:
            connector.discover()
            result = connector.sample(resource, limit=limit)
            value = result.model_dump(mode="json")
            return {**value, "sample_json": exact_json_text(value)}
        finally:
            connector.close()
