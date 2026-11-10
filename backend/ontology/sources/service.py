import json
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import ValidationError

from ontology.connectors import create_connector
from ontology.connectors.policy import NetworkPolicy
from ontology.core.domain import row_dict
from ontology.core.errors import DomainError


def cipher(settings):
    if settings.encryption_key:
        return Fernet(settings.encryption_key.encode())
    path = settings.data_dir / "credential.key"
    try:
        with path.open("xb") as stream:
            stream.write(Fernet.generate_key())
    except FileExistsError:
        pass
    return Fernet(path.read_bytes())


def safe_config(data):
    def check(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in {"password", "token", "secret", "api_key", "authorization"}:
                    raise DomainError(422, "credential_in_config", "凭据须填写在独立凭据字段中")
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
        elif isinstance(value, str) and "://" in value:
            from urllib.parse import urlsplit

            if urlsplit(value).password:
                raise DomainError(422, "credential_in_config", "地址中不得包含密码")

    check(data)
    if len(json.dumps(data)) > 100000:
        raise DomainError(422, "config_too_large", "连接配置过大")


def connector_for(source, settings):
    config = dict(source.config)
    if source.secret:
        config.update(json.loads(cipher(settings).decrypt(source.secret.encode())))
    if source.kind in {"sqlite", "file"}:
        field = "database" if source.kind == "sqlite" else "root"
        root = (settings.data_dir / "imports").resolve()
        path = Path(config.get(field, "")).resolve()
        if not path.is_relative_to(root):
            raise DomainError(422, "source_path_denied", "文件来源必须位于配置的数据目录 imports 内")
    policy = NetworkPolicy(
        frozenset(settings.allowed_source_hosts),
        allow_private_addresses=settings.allow_private_source_addresses,
    )
    try:
        return create_connector(source.kind, config, network_policy=policy)
    except (ValidationError, TypeError, ValueError) as exc:
        raise DomainError(
            422, "invalid_source_config", "连接配置格式不正确，请检查该连接器所需的字段"
        ) from exc


def source_view(source):
    return {**row_dict(source, exclude=("secret",)), "has_credentials": bool(source.secret)}


def require_source_read_identity(source):
    # A shared project Source has no employee binding. Its stored credential
    # cannot authorize the current reader, even when labelled as a user token.
    if source.kind == "feishu":
        raise DomainError(422, "employee_authorization_required", "飞书读取须先配置当前员工授权服务")
