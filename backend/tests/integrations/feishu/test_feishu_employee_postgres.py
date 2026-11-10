"""Real forced-RLS PostgreSQL and concurrent one-use OAuth operations."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ontology.integrations.feishu.gateway import FeishuUserGateway
from ontology.integrations.feishu.models import FeishuAccount, FeishuApplication, FeishuAuthorization
from ontology.integrations.feishu.oauth import FeishuOAuth
from sqlalchemy import select

from tests.support.feishu_employee import BASE, account, application, browse, finish, start
from tests.support.feishu_employee import upstream as upstream  # noqa: PLC0414 - pytest fixture registration
from tests.support.feishu_selection import selection_fixture
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def test_feishu_rls_and_successful_callback(pg_platform, upstream):
    client, app, owner, other = pg_platform
    app_id = application(pg_platform)
    state = start(client, owner, app_id)["state"][0]
    for tenant in (None, other["tenant_id"]):
        with app.state.db.transaction(tenant) as session:
            for model in (FeishuApplication, FeishuAccount, FeishuAuthorization):
                assert session.scalars(select(model)).all() == []
    assert finish(client, state).status_code == 200
    assert browse(client, owner, app_id).status_code == 200
    app.state.db.verify_runtime_role()


def test_concurrent_callbacks_exchange_authorization_code_once(pg_platform, upstream, monkeypatch):
    client, _, owner, _ = pg_platform
    app_id = application(pg_platform)
    state = start(client, owner, app_id)["state"][0]
    original = FeishuOAuth.exchange
    entered, release = threading.Event(), threading.Event()

    async def delayed(self, *args):
        entered.set()
        deadline = time.monotonic() + 15
        while not release.is_set():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        return await original(self, *args)

    monkeypatch.setattr(FeishuOAuth, "exchange", delayed)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(finish, client, state)
        try:
            assert entered.wait(10)
            second = pool.submit(finish, client, state).result(10)
            assert second.status_code == 409, second.text
        finally:
            release.set()
        assert first.result(10).status_code == 200
    assert upstream["exchange"] == 1


def test_concurrent_reads_rotate_refresh_token_once(pg_platform, upstream, monkeypatch):
    client, app, owner, _ = pg_platform
    app_id = application(pg_platform)
    assert finish(client, start(client, owner, app_id)["state"][0]).status_code == 200
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.get(FeishuAccount, account(app, owner).id).expires_at = time.time() - 1
    original = FeishuOAuth.refresh
    entered, release = threading.Event(), threading.Event()

    async def delayed(self, *args):
        entered.set()
        deadline = time.monotonic() + 15
        while not release.is_set():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        return await original(self, *args)

    monkeypatch.setattr(FeishuOAuth, "refresh", delayed)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(browse, client, owner, app_id)
        try:
            assert entered.wait(10)
            second = pool.submit(browse, client, owner, app_id).result(10)
            assert second.status_code == 409, second.text
            assert second.json()["error"]["code"] == "feishu_refresh_in_progress"
        finally:
            release.set()
        assert first.result(10).status_code == 200
    assert upstream["refresh"] == 1


def test_preview_network_wait_does_not_block_revocation_or_publish_late_title(
    pg_platform, upstream, monkeypatch
):
    preview, _ = selection_fixture(pg_platform, monkeypatch)
    client, app, owner, _ = pg_platform
    app_id = account(app, owner).application_id
    original = FeishuUserGateway.get_document
    entered, release = threading.Event(), threading.Event()

    async def delayed(self, *args):
        entered.set()
        deadline = time.monotonic() + 15
        while not release.is_set():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        return await original(self, *args)

    monkeypatch.setattr(FeishuUserGateway, "get_document", delayed)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(preview)
        try:
            assert entered.wait(10)
            revoked = pool.submit(
                client.post,
                BASE + f"/applications/{app_id}/disconnect",
                json={"project_id": owner["project_id"]},
            ).result(10)
            assert revoked.status_code == 200, revoked.text
        finally:
            release.set()
        result = first.result(10)
    assert result.status_code == 409, result.text
    assert "Current private document" not in result.text
