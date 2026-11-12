from concurrent.futures import ThreadPoolExecutor
import time

import pytest
from sqlalchemy import select

from ontology.core.db import Database
from ontology.core.errors import DomainError
from ontology.identity.models import LoginThrottle
from ontology.identity.throttle import bucket, clear_login, reserve_login
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_login_budget_is_atomic_across_app_instances_and_expires(request, fixture_name, monkeypatch):
    _, app, _, _ = request.getfixturevalue(fixture_name)
    other = Database(app.state.settings)

    def attempt(i):
        try:
            reserve_login(other if i % 2 else app.state.db, "synthetic-ip", "tenant", "person")
            return True
        except DomainError as error:
            assert error.status == 429
            return False

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(attempt, range(14)))
        assert sum(results) == 10
        clear_login(app.state.db, bucket("account", "synthetic-ip", "tenant", "person"))
        assert attempt(1)
        future = time.time() + 301
        monkeypatch.setattr("ontology.identity.throttle.time.time", lambda: future)
        assert attempt(0)
        with app.state.db.transaction() as session:
            assert not session.scalar(
                select(LoginThrottle.key_hash).where(LoginThrottle.expires_at <= future)
            )
    finally:
        other.engine.dispose()
