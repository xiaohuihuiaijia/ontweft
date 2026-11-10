"""Session-bound employee authorization and live directory browsing.

Network calls happen outside database transactions. Every result is checked
again against the current platform session, project access and binding revision.
No directory result is persisted or shared with other employees here.
"""

import base64
import hashlib
import json
import secrets
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from cryptography.fernet import InvalidToken
from pydantic import SecretStr
from sqlalchemy import delete, select, update

from ontology.core.errors import DomainError, conflict, forbidden, missing
from ontology.identity.models import LoginSession
from ontology.identity.security import access, audit, token_hash
from ontology.integrations.feishu.gateway import EmployeeBindingContext, FeishuGatewayError, FeishuUserGateway
from ontology.integrations.feishu.models import FeishuAccount, FeishuApplication, FeishuAuthorization
from ontology.integrations.feishu.oauth import READ_SCOPES, FeishuOAuth
from ontology.sources.service import cipher


def reconnect(message="飞书授权已失效，请重新连接当前员工账号"):
    # A remote OAuth failure must not expire the platform login (HTTP 401).
    raise DomainError(409, "feishu_reauthorization_required", message)


def remote_error(exc):
    messages = {
        "permission_denied": "飞书权限不足，请检查应用权限及员工对资源的访问权限",
        "rate_limited": "飞书请求频率受限，请稍后重试目录读取",
        "timed_out": "飞书请求超时，授权操作请重新发起，目录读取可稍后重试",
    }
    raise DomainError(
        422, "feishu_" + exc.code, messages.get(exc.code, "飞书请求未完成，请检查授权或稍后重试")
    ) from None


class EmployeeService:
    def __init__(self, request, user, *, permission="sources:write"):
        self.db, self.settings, self.user = request.app.state.db, request.app.state.settings, user
        self.session_hash = token_hash(request.cookies.get("ontology_session", ""))
        self.request_origin = request.headers.get("origin")
        self.crypt = cipher(self.settings)
        self.permission = permission

    def _guard(self, session, project_id):
        grant = access(session, self.user, project_id, self.permission)
        login = session.get(LoginSession, self.session_hash)
        if (
            not login
            or login.tenant_id != self.user.tenant_id
            or login.user_id != self.user.id
            or login.user_revision != self.user.revision
            or login.expires_at <= time.time()
        ):
            raise DomainError(401, "unauthenticated", "会话已失效，请重新登录")
        return grant

    def _application(self, session, application_id, *, enabled=True):
        app = session.scalar(
            select(FeishuApplication)
            .where(
                FeishuApplication.id == application_id,
                FeishuApplication.tenant_id == self.user.tenant_id,
            )
            .with_for_update()
        )
        if app is None:
            missing()
        if enabled and not app.enabled:
            reconnect("企业飞书应用已停用")
        return app

    def _account(self, session, app):
        return session.scalar(
            select(FeishuAccount)
            .where(
                FeishuAccount.tenant_id == self.user.tenant_id,
                FeishuAccount.application_id == app.id,
                FeishuAccount.user_id == self.user.id,
            )
            .with_for_update()
        )

    def _current(self, session, project_id, app_id, revision, status="active"):
        self._guard(session, project_id)
        app = self._application(session, app_id)
        account = self._account(session, app)
        if (
            not account
            or account.revision != revision
            or account.status != status
            or account.application_revision != app.revision
            or account.user_revision != self.user.revision
        ):
            reconnect("授权状态已改变，请刷新当前账号后重试")
        return app, account

    def _oauth(self, app):
        return FeishuOAuth(
            app.service, app.app_id, SecretStr(self.crypt.decrypt(app.secret.encode()).decode())
        )

    def _pack(self, values):
        return self.crypt.encrypt(json.dumps(values, separators=(",", ":")).encode()).decode()

    def _unpack(self, value):
        return json.loads(self.crypt.decrypt(value.encode()))

    def redirect_uri(self):
        origin = self.settings.feishu_frontend_origin
        parsed = urlsplit(origin)
        if (
            origin not in self.settings.allowed_origins
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or not parsed.hostname
            or (
                parsed.scheme != "https"
                and not (
                    self.settings.environment != "production"
                    and parsed.scheme == "http"
                    and parsed.hostname in {"localhost", "127.0.0.1"}
                )
            )
        ):
            raise DomainError(422, "feishu_callback_unconfigured", "请管理员配置合法的飞书回调前端地址")
        return origin + "/integrations/feishu/callback"

    def _account_view(self, account, app):
        if not account:
            return None
        valid = (
            account.application_revision == app.revision
            and account.user_revision == self.user.revision
            and set(READ_SCOPES).issubset(account.scopes)
        )
        status = account.status if valid and app.enabled else "reauthorize"
        if status == "refreshing" and account.refresh_deadline <= time.time():
            status = "reauthorize"
        if (
            status == "active"
            and account.refresh_expires_at <= time.time()
            and account.expires_at <= time.time()
        ):
            status = "reauthorize"
        return {
            "revision": account.revision,
            "status": status,
            "display_name": account.display_name,
            "external_user_id": account.external_user_id,
            "scopes": account.scopes,
            "expires_at": account.expires_at,
        }

    def list_applications(self, project_id):
        with self.db.transaction(self.user.tenant_id) as session:
            self._guard(session, project_id)
            apps = session.scalars(
                select(FeishuApplication)
                .where(FeishuApplication.tenant_id == self.user.tenant_id)
                .order_by(FeishuApplication.name)
            ).all()
            return {
                "items": [
                    {
                        "id": app.id,
                        "name": app.name,
                        "service": app.service,
                        "app_id": app.app_id,
                        "external_tenant_key": app.external_tenant_key,
                        "revision": app.revision,
                        "enabled": app.enabled,
                        "account": self._account_view(self._account(session, app), app),
                    }
                    for app in apps
                ],
                "required_scopes": READ_SCOPES,
                "redirect_uri": self.redirect_uri(),
            }

    def configure(self, project_id, data, application_id=None):
        with self.db.transaction(self.user.tenant_id) as session:
            grant = self._guard(session, project_id)
            # Re-read admin status inside the transaction; detached login data is insufficient.
            if not session.get(type(self.user), grant.user.id).admin:
                forbidden()
            app = self._application(session, application_id, enabled=False) if application_id else None
            if app:
                if app.revision != data.expected_revision:
                    conflict()
                app.revision += 1
            else:
                app = FeishuApplication(tenant_id=self.user.tenant_id)
                session.add(app)
            for key in ("name", "service", "app_id", "external_tenant_key", "enabled"):
                setattr(app, key, getattr(data, key))
            app.secret = self.crypt.encrypt(data.app_secret.get_secret_value().encode()).decode()
            session.flush()
            # Config changes also physically remove private tokens and pending verifiers.
            accounts = session.scalars(
                select(FeishuAccount).where(
                    FeishuAccount.tenant_id == self.user.tenant_id, FeishuAccount.application_id == app.id
                )
            ).all()
            for account in accounts:
                account.secret, account.status = "", "reauthorize"
                account.revision += 1
                session.execute(
                    delete(FeishuAuthorization).where(FeishuAuthorization.account_id == account.id)
                )
            audit(
                session,
                self.user,
                project_id,
                "feishu.application.configured",
                app.id,
                {"revision": app.revision},
            )
            return {"id": app.id, "revision": app.revision}

    def start(self, project_id, application_id):
        redirect = self.redirect_uri()
        if self.request_origin and self.request_origin != self.settings.feishu_frontend_origin:
            raise DomainError(422, "feishu_origin_mismatch", "请从配置的前端地址登录后连接飞书账号")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        with self.db.transaction(self.user.tenant_id) as session:
            self._guard(session, project_id)
            app = self._application(session, application_id)
            account = self._account(session, app)
            if account is None:
                account = FeishuAccount(
                    tenant_id=self.user.tenant_id, application_id=app.id, user_id=self.user.id
                )
                session.add(account)
            else:
                account.revision += 1
            account.user_revision, account.application_revision = self.user.revision, app.revision
            account.status, account.secret = "pending", ""
            session.flush()
            session.execute(delete(FeishuAuthorization).where(FeishuAuthorization.account_id == account.id))
            session.add(
                FeishuAuthorization(
                    state_hash=token_hash(state),
                    tenant_id=self.user.tenant_id,
                    account_id=account.id,
                    project_id=project_id,
                    session_hash=self.session_hash,
                    account_revision=account.revision,
                    verifier=self.crypt.encrypt(verifier.encode()).decode(),
                    redirect_uri=redirect,
                    expires_at=time.time() + 300,
                )
            )
            result = self._oauth(app).authorization_url(
                state=state, challenge=challenge, redirect_uri=redirect
            )
            audit(session, self.user, project_id, "feishu.authorization.started", app.id)
        return {"authorization_url": result}

    def revoke(self, project_id, application_id):
        with self.db.transaction(self.user.tenant_id) as session:
            self._guard(session, project_id)
            app = self._application(session, application_id, enabled=False)
            account = self._account(session, app)
            if account:
                account.secret, account.status = "", "revoked"
                account.revision += 1
                session.execute(
                    delete(FeishuAuthorization).where(FeishuAuthorization.account_id == account.id)
                )
            audit(session, self.user, project_id, "feishu.account.disconnected", app.id)
        return {"disconnected": True}

    def _save_tokens(self, account, tokens, started_at):
        account.secret = self._pack(
            {
                "access": tokens.access_token.get_secret_value(),
                "refresh": tokens.refresh_token.get_secret_value(),
            }
        )
        account.expires_at = started_at + tokens.expires_in
        account.refresh_expires_at = started_at + tokens.refresh_expires_in
        account.scopes, account.status = list(tokens.scopes), "active"
        account.refresh_deadline = 0
        account.revision += 1

    def _fail(self, app_id, revision):
        # Does not require a live session: only clear this exact in-flight generation.
        with self.db.transaction(self.user.tenant_id) as session:
            session.execute(
                update(FeishuAccount)
                .where(
                    FeishuAccount.tenant_id == self.user.tenant_id,
                    FeishuAccount.user_id == self.user.id,
                    FeishuAccount.application_id == app_id,
                    FeishuAccount.revision == revision,
                )
                .values(secret="", status="reauthorize", revision=revision + 1)
            )

    async def complete(self, state, code, denied=False):
        with self.db.transaction(self.user.tenant_id) as session:
            attempt = session.scalar(
                select(FeishuAuthorization).where(
                    FeishuAuthorization.state_hash == token_hash(state),
                    FeishuAuthorization.tenant_id == self.user.tenant_id,
                    FeishuAuthorization.session_hash == self.session_hash,
                )
            )
            if not attempt or attempt.consumed or attempt.expires_at <= time.time():
                reconnect("授权回调已失效或不属于当前会话，请重新连接")
            account = session.get(FeishuAccount, attempt.account_id)
            if not account or account.user_id != self.user.id:
                missing()
            project_id, app_id, revision = (
                attempt.project_id,
                account.application_id,
                attempt.account_revision,
            )
            app, account = self._current(session, project_id, app_id, revision, "pending")
            # CAS also enforces one use on SQLite, where FOR UPDATE is ignored.
            consumed = session.execute(
                update(FeishuAuthorization)
                .where(
                    FeishuAuthorization.state_hash == attempt.state_hash,
                    FeishuAuthorization.consumed.is_(False),
                )
                .values(consumed=True, verifier="")
                .execution_options(synchronize_session=False)
            )
            if consumed.rowcount != 1:
                reconnect()
            verifier = SecretStr(self.crypt.decrypt(attempt.verifier.encode()).decode())
            redirect = attempt.redirect_uri
            oauth = self._oauth(app)
        if denied:
            self._fail(app_id, revision)
            reconnect("已取消飞书授权，可以重新连接")
        started = time.time()
        try:
            tokens = await oauth.exchange(code, verifier, redirect)
            identity = await oauth.identity(tokens.access_token)
            with self.db.transaction(self.user.tenant_id) as session:
                app, account = self._current(session, project_id, app_id, revision, "pending")
                if identity.tenant_key != app.external_tenant_key:
                    reconnect("授权账号不属于配置的飞书企业，请切换账号后重新连接")
                account.external_user_id, account.display_name = identity.open_id, identity.name
                self._save_tokens(account, tokens, started)
                audit(session, self.user, project_id, "feishu.account.connected", app.id)
            return {"connected": True, "project_id": project_id}
        except (FeishuGatewayError, DomainError) as exc:
            self._fail(app_id, revision)
            if isinstance(exc, FeishuGatewayError):
                remote_error(exc)
            raise

    async def context(self, project_id, application_id):
        with self.db.transaction(self.user.tenant_id) as session:
            self._guard(session, project_id)
            app = self._application(session, application_id)
            account = self._account(session, app)
            if (
                not account
                or account.user_revision != self.user.revision
                or account.application_revision != app.revision
                or not set(READ_SCOPES).issubset(account.scopes)
            ):
                reconnect()
            if account.status == "refreshing" and account.refresh_deadline > time.time():
                raise DomainError(409, "feishu_refresh_in_progress", "当前账号正在刷新授权，请稍后重试")
            if account.status != "active" or not account.secret:
                reconnect()
            if account.expires_at > time.time() + 60:
                return self._context(app, account), app.service
            if account.refresh_expires_at <= time.time():
                reconnect()
            oauth = self._oauth(app)
            refresh_token = SecretStr(self._unpack(account.secret)["refresh"])
            revision = account.revision + 1
            # Commit the single-use refresh reservation before sending any request.
            result = session.execute(
                update(FeishuAccount)
                .where(
                    FeishuAccount.id == account.id,
                    FeishuAccount.revision == account.revision,
                    FeishuAccount.status == "active",
                )
                .values(status="refreshing", secret="", revision=revision, refresh_deadline=time.time() + 90)
            )
            if result.rowcount != 1:
                reconnect("授权正在变化，请稍后重试")
        started = time.time()
        try:
            tokens = await oauth.refresh(refresh_token)
            identity = await oauth.identity(tokens.access_token)
            with self.db.transaction(self.user.tenant_id) as session:
                app, account = self._current(session, project_id, application_id, revision, "refreshing")
                if (
                    identity.tenant_key != app.external_tenant_key
                    or identity.open_id != account.external_user_id
                ):
                    reconnect()
                self._save_tokens(account, tokens, started)
                return self._context(app, account), app.service
        except (FeishuGatewayError, DomainError) as exc:
            self._fail(application_id, revision)
            if isinstance(exc, FeishuGatewayError):
                remote_error(exc)
            raise

    def _context(self, app, account):
        return EmployeeBindingContext(
            platform_tenant_id=self.user.tenant_id,
            user_id=self.user.id,
            connection_id=app.id,
            external_tenant_key=app.external_tenant_key,
            external_user_id=account.external_user_id,
            credential_revision=account.revision,
            user_access_token=SecretStr(self._unpack(account.secret)["access"]),
            expires_at=datetime.fromtimestamp(account.expires_at, UTC),
        )

    async def browse(self, project_id, application_id, *, space_id=None, parent_node=None, cursor=None):
        context, service = await self.context(project_id, application_id)
        scope = {
            "tenant": self.user.tenant_id,
            "user": self.user.id,
            "session": self.session_hash,
            "project": project_id,
            "application": application_id,
            "revision": context.credential_revision,
            "space": space_id,
            "parent": parent_node,
        }
        page_token = None
        if cursor:
            try:
                decoded = self._unpack(cursor)
                if decoded["scope"] != scope or decoded["expires"] <= time.time():
                    raise ValueError
                page_token = decoded["page"]
            except (InvalidToken, ValueError, TypeError, KeyError):
                raise DomainError(
                    409, "feishu_cursor_invalid", "目录分页已失效，请重新加载当前目录"
                ) from None
        gateway = FeishuUserGateway(context, service=service)
        try:
            page = (
                await gateway.list_nodes(space_id, parent_node_token=parent_node, page_token=page_token)
                if space_id
                else await gateway.list_spaces(page_token=page_token)
            )
        except FeishuGatewayError as exc:
            if exc.code in {"credential_invalid", "credential_expired"}:
                self._fail(application_id, context.credential_revision)
            remote_error(exc)
        with self.db.transaction(self.user.tenant_id) as session:
            self._current(session, project_id, application_id, context.credential_revision)
            if context.expires_at.timestamp() <= time.time():
                reconnect()
        # Only directory fields required by the UI. Never leak SDK response extras.
        keys = (
            ("node_token", "obj_type", "title", "has_child", "node_type")
            if space_id
            else ("space_id", "name", "description")
        )
        for item in page.items:
            for key in keys:
                if key not in item:
                    continue
                value = item[key]
                if (key == "has_child" and type(value) is not bool) or (
                    key != "has_child" and (not isinstance(value, str) or len(value) > 16384)
                ):
                    remote_error(FeishuGatewayError("invalid_response"))
        return {
            "items": [{key: item[key] for key in keys if key in item} for item in page.items],
            "account_revision": context.credential_revision,
            "has_more": page.has_more,
            "next_cursor": self._pack(
                {"scope": scope, "expires": time.time() + 300, "page": page.next_page_token}
            )
            if page.has_more
            else None,
        }
