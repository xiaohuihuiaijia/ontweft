import secrets
import time

from fastapi import Depends, Request, Response
from sqlalchemy import delete, select, text

from ontology.core.domain import row_dict
from ontology.core.errors import DomainError, forbidden, missing
from ontology.identity.models import LoginSession, Membership, Project, Tenant, User
from ontology.identity.schemas import LoginInput, MemberInput, ProjectInput, UserInput
from ontology.identity.security import (
    DUMMY_PASSWORD,
    PASSWORDS,
    ROLES,
    access,
    audit,
    authenticated,
    token_hash,
    verify_password,
)


def register_identity_routes(app, database, settings):
    @app.post("/api/auth/login")
    def login(data: LoginInput, request: Request, response: Response):
        address = request.client.host if request.client else "unknown"
        identifier = (data.username if data.username is not None else data.email).strip().lower()
        with database.transaction() as session:
            # Resolve the enterprise before querying RLS-protected users. Never
            # search all enterprises for a matching name or password.
            tenant_slug = settings.login_tenant_slug or data.tenant_slug
            if tenant_slug:
                tenant = session.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
            else:
                tenants = session.scalars(select(Tenant).limit(2)).all()
                tenant = tenants[0] if len(tenants) == 1 else None
            if settings.login_tenant_slug and data.tenant_slug not in (None, settings.login_tenant_slug):
                tenant = None
            rate_key = (address, tenant_slug or "", identifier)
            attempts = [t for t in app.state.login_attempts.get(rate_key, []) if t > time.time() - 300]
            if len(attempts) >= 10:
                raise DomainError(429, "rate_limited", "登录失败次数过多，请稍后重试")
            if tenant and database.engine.dialect.name == "postgresql":
                session.execute(
                    text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant.id}
                )
            user = (
                session.scalar(
                    select(User).where(
                        User.tenant_id == tenant.id,
                        User.email == identifier
                        if "@" in identifier or data.email is not None
                        else User.username == identifier,
                    )
                )
                if tenant
                else None
            )
            valid = verify_password(user.password_hash if user else DUMMY_PASSWORD, data.password)
            if not user or not valid or not user.active:
                app.state.login_attempts[rate_key] = attempts + [time.time()]
                raise DomainError(401, "invalid_credentials", "用户名或密码不正确")
            app.state.login_attempts.pop(rate_key, None)
            if database.engine.dialect.name == "postgresql":
                session.execute(
                    text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": user.tenant_id}
                )
            token = secrets.token_urlsafe(48)
            session.add(
                LoginSession(
                    token_hash=token_hash(token),
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    user_revision=user.revision,
                    expires_at=time.time() + settings.session_hours * 3600,
                )
            )
            audit(session, user, None, "session.created", user.id)
            response.set_cookie(
                "ontology_session",
                token,
                max_age=settings.session_hours * 3600,
                httponly=True,
                secure=settings.secure_cookies,
                samesite="strict",
                path="/",
            )
        return {"user": row_dict(user, exclude=("password_hash",))}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, user=Depends(authenticated)):
        with database.transaction(user.tenant_id) as session:
            session.execute(
                delete(LoginSession).where(
                    LoginSession.token_hash == token_hash(request.cookies.get("ontology_session", ""))
                )
            )
        response.delete_cookie("ontology_session", path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(user=Depends(authenticated)):
        with database.transaction(user.tenant_id) as session:
            memberships = session.scalars(
                select(Membership).where(
                    Membership.user_id == user.id, Membership.tenant_id == user.tenant_id
                )
            ).all()
            projects = []
            permissions = set()
            for member in memberships:
                project = session.get(Project, member.project_id)
                projects.append(
                    {**row_dict(project), "role": member.role, "permissions": sorted(ROLES[member.role])}
                )
                permissions.update(ROLES[member.role])
            tenant = session.get(Tenant, user.tenant_id)
            return {
                "user": row_dict(user, exclude=("password_hash",)),
                "tenant": row_dict(tenant),
                "projects": projects,
                "permissions": sorted(permissions),
            }

    @app.get("/api/projects")
    def projects(user=Depends(authenticated)):
        result = me(user)
        return {"items": result["projects"], "total": len(result["projects"])}

    @app.post("/api/projects", status_code=201)
    def create_project(data: ProjectInput, user=Depends(authenticated)):
        if not user.admin:
            forbidden()
        with database.transaction(user.tenant_id) as session:
            project = Project(tenant_id=user.tenant_id, name=data.name)
            session.add(project)
            session.flush()
            session.add(
                Membership(tenant_id=user.tenant_id, project_id=project.id, user_id=user.id, role="owner")
            )
            audit(session, user, project.id, "project.created", project.id)
            return row_dict(project)

    @app.get("/api/users")
    def users(user=Depends(authenticated)):
        if not user.admin:
            forbidden()
        with database.transaction(user.tenant_id) as session:
            items = session.scalars(select(User).where(User.tenant_id == user.tenant_id)).all()
            return {"items": [row_dict(x, exclude=("password_hash",)) for x in items], "total": len(items)}

    @app.post("/api/users", status_code=201)
    def create_user(data: UserInput, user=Depends(authenticated)):
        if not user.admin:
            forbidden()
        with database.transaction(user.tenant_id) as session:
            person = User(
                tenant_id=user.tenant_id,
                username=data.username,
                email=data.email.lower(),
                display_name=data.display_name,
                password_hash=PASSWORDS.hash(data.password),
            )
            session.add(person)
            session.flush()
            audit(session, user, None, "user.created", person.id)
            return row_dict(person, exclude=("password_hash",))

    @app.post("/api/users/{user_id}/disable")
    def disable_user(user_id: str, user=Depends(authenticated)):
        if not user.admin or user_id == user.id:
            forbidden()
        with database.transaction(user.tenant_id) as session:
            target = session.scalar(select(User).where(User.tenant_id == user.tenant_id, User.id == user_id))
            if not target:
                missing()
            target.active, target.revision = False, target.revision + 1
            session.execute(delete(LoginSession).where(LoginSession.user_id == target.id))
            audit(session, user, None, "user.disabled", target.id)
            return {"ok": True}

    @app.get("/api/members")
    def members(project_id: str, user=Depends(authenticated)):
        with database.transaction(user.tenant_id) as session:
            access(session, user, project_id, "members:manage")
            items = session.execute(
                select(Membership, User)
                .join(User, Membership.user_id == User.id)
                .where(Membership.project_id == project_id, Membership.tenant_id == user.tenant_id)
            ).all()
            return {
                "items": [
                    {**row_dict(m), "display_name": u.display_name, "email": u.email} for m, u in items
                ],
                "total": len(items),
            }

    @app.post("/api/members")
    def set_member(data: MemberInput, user=Depends(authenticated)):
        with database.transaction(user.tenant_id) as session:
            access(session, user, data.project_id, "members:manage")
            target = session.scalar(
                select(User).where(
                    User.id == data.user_id, User.tenant_id == user.tenant_id, User.active.is_(True)
                )
            )
            if not target:
                missing()
            if data.user_id == user.id and data.role != "owner":
                raise DomainError(422, "self_demotion", "请由另一位项目管理员调整您的角色")
            member = session.scalar(
                select(Membership).where(
                    Membership.project_id == data.project_id, Membership.user_id == target.id
                )
            )
            if member:
                member.role = data.role
            else:
                member = Membership(
                    tenant_id=user.tenant_id, project_id=data.project_id, user_id=target.id, role=data.role
                )
                session.add(member)
            session.flush()
            audit(session, user, data.project_id, "membership.changed", member.id, {"role": data.role})
            return row_dict(member)
