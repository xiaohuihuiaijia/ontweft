"""Adding optional usernames must preserve existing accounts and tenant policies."""

import pytest
from sqlalchemy import inspect, text

from tests.support.cdc_evidence_migrations import isolated_database
from tests.support.identity import bootstrap_historical


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_username_upgrade_preserves_existing_identity(tmp_path, dialect):
    with isolated_database(tmp_path, dialect) as (db, migrate, _):
        migrate("d52e0c98a46b")
        owner = bootstrap_historical(
            db,
            tenant_slug="original",
            tenant_name="Original",
            email="owner@example.test",
            password="Synthetic-Migration-Password",
        )
        with db.transaction(owner["tenant_id"]) as session:
            old = dict(session.execute(text("SELECT * FROM users")).mappings().one())
        migrate("b4a2d7e9c601")
        with db.transaction(owner["tenant_id"]) as session:
            new = dict(session.execute(text("SELECT * FROM users")).mappings().one())
            assert new.pop("username") is None
            assert new == old
            if dialect == "postgresql":
                assert session.execute(
                    text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='users'")
                ).one() == (True, True)
        assert any(
            index["name"] == "uq_user_username" and index["unique"]
            for index in inspect(db.engine).get_indexes("users")
        )
