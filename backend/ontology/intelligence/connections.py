"""Connection ownership, credential protection and model selection."""

from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import InvalidToken
from sqlalchemy import select

from ontology.core.errors import DomainError, conflict, forbidden, missing
from ontology.identity.models import User
from ontology.intelligence.models import ModelConnection
from ontology.intelligence.schemas import ConnectionInput, ModelSpec
from ontology.sources.service import cipher, safe_config

PARAMETERS = {
    "temperature", "top_p", "seed", "presence_penalty", "frequency_penalty",
    "enable_thinking", "thinking_budget",
}


def require_admin(session, user):
    current = session.scalar(
        select(User).where(User.tenant_id == user.tenant_id, User.id == user.id)
        .with_for_update(key_share=True).execution_options(populate_existing=True)
    )
    if not current or not current.active or current.revision != user.revision:
        raise DomainError(401, "unauthenticated", "账号或会话已失效")
    if not current.admin:
        forbidden()


def validate_connection(data: ConnectionInput) -> str:
    parsed = urlsplit(data.base_url)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme not in ({"https", "http"} if data.allow_private_network else {"https"})
        or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or port == -1
    ):
        raise DomainError(422, "invalid_model_endpoint", "请填写不含凭据、查询参数的模型服务地址")
    ids = [model.id for model in data.models]
    if len(set(ids)) != len(ids) or set(data.roles) != {"extraction", "synthesis"}:
        raise DomainError(422, "invalid_model_roles", "模型不可重复，须配置提取和综合两个用途")
    if any(model not in ids for model in data.roles.values()):
        raise DomainError(422, "unknown_model", "用途绑定的模型不在允许列表中")
    for model in data.models:
        safe_config(model.parameters)
        if set(model.parameters) - PARAMETERS:
            raise DomainError(422, "unsupported_model_parameter", "仅支持采样及思考参数，不允许覆盖工具或请求内容")
        thinking_budget = model.parameters.get("thinking_budget")
        if thinking_budget is not None and (
            isinstance(thinking_budget, bool) or not isinstance(thinking_budget, int)
            or not 0 <= thinking_budget <= 131072
        ):
            raise DomainError(422, "invalid_thinking_budget", "思考上限须为 0 至 131072 的整数")
        if model.parameters.get("enable_thinking") is True and not thinking_budget:
            raise DomainError(422, "thinking_budget_required", "开启思考模式须配置明确的思考 Token 上限")
        if not model.text or model.max_output_tokens >= model.context_window:
            raise DomainError(422, "invalid_model_limits", "模型须支持文字，输出上限须小于上下文上限")
    if data.api_key is not None and len(data.api_key.get_secret_value()) > 4096:
        raise DomainError(422, "invalid_model_credential", "模型凭据长度不正确")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def connection_view(row, *, admin=False):
    result = {
        "id": row.id, "name": row.name, "revision": row.revision,
        "enabled": row.enabled, "models": row.models, "roles": row.roles,
    }
    if admin:
        result.update(
            base_url=row.base_url, allow_private_network=row.allow_private_network,
            has_credentials=bool(row.secret), last_test=row.last_test,
            created_at=row.created_at, updated_at=row.updated_at,
        )
    return result


def get_connection(session, tenant_id, connection_id, *, enabled=False, revision=None):
    row = session.scalar(
        select(ModelConnection).where(
            ModelConnection.tenant_id == tenant_id, ModelConnection.id == connection_id
        )
    )
    if not row:
        missing()
    if enabled and not row.enabled:
        raise DomainError(409, "model_connection_disabled", "模型连接已停用")
    if revision is not None and row.revision != revision:
        conflict("模型配置已变更，请刷新后重试")
    return row


def model_for(row, role) -> ModelSpec:
    if not row.enabled:
        raise DomainError(409, "model_connection_disabled", "模型连接已停用")
    model_id = row.roles.get(role)
    for value in row.models:
        if value.get("id") == model_id:
            return ModelSpec.model_validate(value)
    raise DomainError(422, "unknown_model", "尚未配置此用途的模型")


def decrypt_key(row, settings):
    if not row.secret:
        return ""
    try:
        return cipher(settings).decrypt(row.secret.encode()).decode()
    except (InvalidToken, ValueError, UnicodeError):
        raise DomainError(409, "model_credential_unavailable", "模型凭据无法读取，请管理员重新配置") from None
