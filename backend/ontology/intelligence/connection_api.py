"""Tenant administrators configure connections; users select the enabled catalog."""

import base64
import io
import secrets
from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from ontology.core.errors import conflict
from ontology.core.models import now
from ontology.identity.security import audit, authenticated
from ontology.intelligence.client import ModelCallError, ModelClient
from ontology.intelligence.connections import (
    connection_view,
    get_connection,
    model_for,
    require_admin,
    validate_connection,
)
from ontology.intelligence.models import ModelConnection
from ontology.intelligence.schemas import ConnectionInput, ConnectionTest, ConnectionUpdate
from ontology.sources.service import cipher

router = APIRouter()


@router.get("/model-connections")
def list_connections(request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        query = select(ModelConnection).where(ModelConnection.tenant_id == user.tenant_id)
        if user.admin:
            require_admin(session, user)
        else:
            query = query.where(ModelConnection.enabled.is_(True))
        rows = session.scalars(query.order_by(ModelConnection.created_at)).all()
        return {"items": [connection_view(row, admin=user.admin) for row in rows], "total": len(rows)}


def apply_input(row, data, settings):
    row.name = data.name
    row.base_url = validate_connection(data)
    row.enabled = data.enabled
    row.allow_private_network = data.allow_private_network
    row.models = [model.model_dump() for model in data.models]
    row.roles = data.roles
    if getattr(data, "clear_credentials", False):
        row.secret = ""
    if data.api_key and data.api_key.get_secret_value().strip():
        row.secret = cipher(settings).encrypt(data.api_key.get_secret_value().strip().encode()).decode()


@router.post("/model-connections", status_code=201)
def create_connection(data: ConnectionInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        require_admin(session, user)
        row = ModelConnection(tenant_id=user.tenant_id, secret="")
        apply_input(row, data, request.app.state.settings)
        session.add(row)
        session.flush()
        audit(session, user, None, "model_connection.created", row.id)
        return connection_view(row, admin=True)


@router.put("/model-connections/{connection_id}")
@router.patch("/model-connections/{connection_id}")
def update_connection(
    connection_id: str, data: ConnectionUpdate, request: Request, user=Depends(authenticated)
):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        require_admin(session, user)
        row = get_connection(session, user.tenant_id, connection_id, revision=data.expected_revision)
        session.refresh(row, with_for_update=True)
        if row.revision != data.expected_revision:
            conflict()
        apply_input(row, data, request.app.state.settings)
        row.revision += 1
        row.updated_at = now()
        row.last_test = {}
        audit(session, user, None, "model_connection.updated", row.id, {"revision": row.revision})
        return connection_view(row, admin=True)


@router.delete("/model-connections/{connection_id}")
def disable_connection(
    connection_id: str, request: Request, expected_revision: int = Query(ge=1), user=Depends(authenticated)
):
    # Preserve referenced configurations and old run records rather than deleting history.
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        require_admin(session, user)
        row = get_connection(session, user.tenant_id, connection_id, revision=expected_revision)
        session.refresh(row, with_for_update=True)
        if row.revision != expected_revision:
            conflict()
        row.enabled = False
        row.revision += 1
        row.updated_at = now()
        audit(session, user, None, "model_connection.disabled", row.id)
        return connection_view(row, admin=True)


def probe(client, role):
    model = model_for(client.connection, role)
    token = secrets.token_hex(4)
    colour = "none"
    images = []
    if model.vision:
        from PIL import Image

        colour = secrets.choice(["red", "green", "blue"])
        output = io.BytesIO()
        Image.new("RGB", (48, 48), colour).save(output, format="PNG")
        images = ["data:image/png;base64," + base64.b64encode(output.getvalue()).decode()]
    schema = {
        "type": "object", "properties": {
            "token": {"type": "string"},
            "colour": {"type": "string", "enum": ["red", "green", "blue", "none"]},
        }, "required": ["token", "colour"], "additionalProperties": False,
    }
    result = client.generate(
        role, instruction="Return the requested token and the dominant colour of the image. "
        "Use none if no image was provided.", text=f"Token: {token}", images=images,
        schema=schema, max_output_tokens=1024,
    )
    checks = {
        "text": result.data.get("token") == token,
        "structured": True,
        "vision": result.data.get("colour") == colour if images else None,
    }
    return {
        "ok": checks["text"] and checks["vision"] is not False,
        "checks": checks,
        **{key: value for key, value in asdict(result).items() if key != "data"},
    }


@router.post("/model-connections/{connection_id}/test")
def test_connection(
    connection_id: str, data: ConnectionTest, request: Request, user=Depends(authenticated)
):
    db = request.app.state.db
    with db.transaction(user.tenant_id) as session:
        require_admin(session, user)
        baseline = get_connection(
            session, user.tenant_id, connection_id, enabled=True, revision=data.expected_revision
        )
    client = ModelClient(baseline, request.app.state.settings)
    try:
        result = probe(client, data.role)
    except ModelCallError as exc:
        result = {"ok": False, "error": {"code": exc.code, "message": exc.message}, "usage": exc.usage}
    with db.transaction(user.tenant_id, write=True) as session:
        require_admin(session, user)
        row = get_connection(session, user.tenant_id, connection_id, revision=data.expected_revision)
        session.refresh(row, with_for_update=True)
        if row.revision != data.expected_revision:
            conflict("测试期间模型配置已变更，请重新测试")
        row.last_test = {**result, "role": data.role, "tested_at": now(), "revision": row.revision}
        audit(session, user, None, "model_connection.tested", row.id, {"ok": result["ok"], "role": data.role})
    return result
