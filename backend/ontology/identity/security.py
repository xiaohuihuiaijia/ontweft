import hashlib
import secrets
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Request
from sqlalchemy import select, text

from ontology.audit.models import Audit
from ontology.core.errors import DomainError, forbidden, missing
from ontology.identity.models import LoginSession, Membership, Project, User

PASSWORDS = PasswordHasher()
DUMMY_PASSWORD = PASSWORDS.hash(secrets.token_urlsafe(32))
ROLES = {
    "owner": {
        "objects:read",
        "objects:write",
        "ontology:write",
        "ontology:approve",
        "ontology:publish",
        "sources:write",
        "documents:write",
        "knowledge:write",
        "knowledge:publish",
        "jobs:manage",
        "audit:read",
        "members:manage",
        "actions:request",
        "actions:approve",
        "actions:execute",
    },
    "modeler": {
        "objects:read",
        "objects:write",
        "ontology:write",
        "sources:write",
        "documents:write",
        "knowledge:write",
        "actions:request",
    },
    "reviewer": {
        "objects:read",
        "ontology:approve",
        "ontology:publish",
        "knowledge:publish",
        "actions:approve",
        "audit:read",
    },
    "operator": {
        "objects:read",
        "objects:write",
        "documents:write",
        "knowledge:write",
        "jobs:manage",
        "actions:request",
        "actions:execute",
    },
    "member": {"objects:read", "knowledge:write", "actions:request"},
    "auditor": {"objects:read", "audit:read"},
}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_password(encoded: str, password: str) -> bool:
    try:
        return PASSWORDS.verify(encoded, password)
    except VerificationError:
        return False


@dataclass
class Access:
    user: User
    project: Project
    role: str
    permissions: set[str]


def authenticated(request: Request) -> User:
    token = request.cookies.get("ontology_session", "")
    if not token:
        raise DomainError(401, "unauthenticated", "请登录")
    with request.app.state.db.transaction(session_hash=token_hash(token)) as session:
        login = session.get(LoginSession, token_hash(token))
        if login and session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": login.tenant_id}
            )
        user = session.get(User, login.user_id) if login else None
        if (
            not login
            or not user
            or login.tenant_id != user.tenant_id
            or not user.active
            or login.expires_at <= time.time()
            or login.user_revision != user.revision
        ):
            raise DomainError(401, "unauthenticated", "会话已失效，请重新登录")
        return user


def access(session, user: User, project_id: str, permission="objects:read") -> Access:
    query = select(Project).where(Project.id == project_id, Project.tenant_id == user.tenant_id)
    if permission != "objects:read" and permission != "audit:read":
        query = query.with_for_update()
    project = session.scalar(query)
    user_query = select(User).where(User.id == user.id, User.tenant_id == user.tenant_id)
    if permission not in {"objects:read", "audit:read"}:
        user_query = user_query.with_for_update(key_share=True)
    current_user = session.scalar(user_query)
    if not current_user or not current_user.active or current_user.revision != user.revision:
        raise DomainError(401, "unauthenticated", "账号或会话已失效")
    member = session.scalar(
        select(Membership).where(
            Membership.project_id == project_id,
            Membership.user_id == user.id,
            Membership.tenant_id == user.tenant_id,
        )
    )
    if project is None or member is None:
        missing()
    permissions = ROLES.get(member.role, set())
    if permission not in permissions:
        forbidden()
    return Access(user, project, member.role, permissions)


def audit(session, user: User, project_id: str | None, event: str, resource_id: str, details=None):
    session.add(
        Audit(
            tenant_id=user.tenant_id,
            project_id=project_id,
            actor_id=user.id,
            event=event,
            resource_id=resource_id,
            details=details or {},
        )
    )


def allowed_field(prop: dict, role: str, operation="read") -> bool:
    roles = prop.get(f"{operation}_roles", [])
    return not roles or role in roles
