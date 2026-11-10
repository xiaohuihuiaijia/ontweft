"""Official SDK transport for OAuth v3; no token cache or automatic retries.

The SDK's synchronous AccessToken helper follows redirects and parses JSON before
HTTP errors. Use its BaseRequest/async transport with the documented v3 contract
instead. A code or rotating refresh token may have been consumed on timeout.
"""

import json
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from ontology.integrations.feishu.gateway import (
    OFFICIAL_DOMAINS,
    FeishuGatewayError,
    _finite_json_float,
    _reject_json_constant,
    _text,
    _unique_json_object,
)

OAUTH_DOMAINS = {"feishu": "https://accounts.feishu.cn", "lark": "https://accounts.larksuite.com"}
# No contact, email, phone or write scopes. Actual granted scopes are verified.
READ_SCOPES = (
    "wiki:wiki:readonly",
    "docx:document:readonly",
    "docs:permission.member:auth",
    "offline_access",
)


@dataclass(frozen=True)
class OAuthTokens:
    access_token: SecretStr = field(repr=False)
    refresh_token: SecretStr = field(repr=False)
    expires_in: int
    refresh_expires_in: int
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class EmployeeIdentity:
    tenant_key: str
    open_id: str
    name: str


class FeishuOAuth:
    def __init__(self, service: str, app_id: str, app_secret: SecretStr):
        if service not in OAUTH_DOMAINS or not isinstance(app_secret, SecretStr):
            raise FeishuGatewayError("invalid_argument")
        self.service, self.app_id, self._secret = service, _text(app_id), app_secret
        # Registering core routes must not import the entire generated SDK.
        import lark_oapi as lark

        self._client = lark.Client.builder().domain(OFFICIAL_DOMAINS[service]).timeout(20).build()
        self._token_url = OAUTH_DOMAINS[service] + "/oauth/v3/token"

    def authorization_url(self, *, state: str, challenge: str, redirect_uri: str) -> str:
        return (
            OAUTH_DOMAINS[self.service]
            + "/open-apis/authen/v1/authorize?"
            + urlencode(
                {
                    "client_id": self.app_id,
                    "response_type": "code",
                    "redirect_uri": redirect_uri,
                    "scope": " ".join(READ_SCOPES),
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "prompt": "consent",
                }
            )
        )

    async def _call(self, request, option=None) -> dict:
        from lark_oapi.core.http.transport import Transport

        try:
            raw = await Transport.aexecute(self._client._config, request, option)
        except httpx.TimeoutException:
            raise FeishuGatewayError("timed_out") from None
        except httpx.HTTPError:
            raise FeishuGatewayError("transport_error") from None
        except Exception:  # noqa: BLE001 - sanitize SDK exceptions containing credentials.
            raise FeishuGatewayError("upstream_rejected") from None
        if raw is None or type(raw.status_code) is not int or not isinstance(raw.content, bytes):
            raise FeishuGatewayError("invalid_response")
        status = raw.status_code
        if 300 <= status < 400:
            raise FeishuGatewayError("redirect_rejected")
        if status == 429:
            raise FeishuGatewayError("rate_limited")
        if status >= 500:
            raise FeishuGatewayError("upstream_unavailable")
        if len(raw.content) > 65536:
            raise FeishuGatewayError("response_budget")
        try:
            data = json.loads(
                raw.content,
                parse_constant=_reject_json_constant,
                parse_float=_finite_json_float,
                object_pairs_hook=_unique_json_object,
            )
        except (ValueError, UnicodeError):
            raise FeishuGatewayError("invalid_response") from None
        if not isinstance(data, dict) or type(data.get("code")) is not int:
            raise FeishuGatewayError("invalid_response")
        if status != 200 or data["code"] != 0:
            raise FeishuGatewayError("credential_invalid")
        return data

    async def _tokens(self, body: dict) -> OAuthTokens:
        from lark_oapi.core.enum import HttpMethod
        from lark_oapi.core.model import BaseRequest

        request = (
            BaseRequest.builder()
            .http_method(HttpMethod.POST)
            .uri(self._token_url)
            .headers({"Content-Type": "application/json; charset=utf-8"})
            .body(
                {
                    **body,
                    "client_id": self.app_id,
                    "client_secret": self._secret.get_secret_value(),
                    "scope": " ".join(READ_SCOPES),
                }
            )
            .build()
        )
        data = await self._call(request)
        for key in ("expires_in", "refresh_token_expires_in"):
            if type(data.get(key)) is not int or not 0 < data[key] <= 366 * 86400:
                raise FeishuGatewayError("invalid_response")
        scopes = data.get("scope")
        if not isinstance(scopes, str) or len(scopes) > 16384 or data.get("token_type") != "Bearer":
            raise FeishuGatewayError("invalid_response")
        if not set(READ_SCOPES).issubset(scopes.split()):
            raise FeishuGatewayError("permission_denied")
        tokens = []
        for key in ("access_token", "refresh_token"):
            token = _text(data.get(key), limit=8192, code="invalid_response")
            if any(not 33 <= ord(c) <= 126 for c in token):
                raise FeishuGatewayError("invalid_response")
            tokens.append(SecretStr(token))
        return OAuthTokens(
            *tokens, data["expires_in"], data["refresh_token_expires_in"], tuple(scopes.split())
        )

    async def exchange(self, code: SecretStr, verifier: SecretStr, redirect_uri: str) -> OAuthTokens:
        return await self._tokens(
            {
                "grant_type": "authorization_code",
                "code": code.get_secret_value(),
                "code_verifier": verifier.get_secret_value(),
                "redirect_uri": redirect_uri,
            }
        )

    async def refresh(self, refresh_token: SecretStr) -> OAuthTokens:
        return await self._tokens(
            {"grant_type": "refresh_token", "refresh_token": refresh_token.get_secret_value()}
        )

    async def identity(self, access_token: SecretStr) -> EmployeeIdentity:
        import lark_oapi as lark
        from lark_oapi.api.authen.v1 import GetUserInfoRequest

        request = GetUserInfoRequest.builder().build()
        option = lark.RequestOption.builder().user_access_token(access_token.get_secret_value()).build()
        payload = await self._call(request, option)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise FeishuGatewayError("invalid_response")
        return EmployeeIdentity(
            _text(data.get("tenant_key"), limit=200, code="invalid_response"),
            _text(data.get("open_id"), limit=200, code="invalid_response"),
            _text(data.get("name"), limit=200, code="invalid_response"),
        )
