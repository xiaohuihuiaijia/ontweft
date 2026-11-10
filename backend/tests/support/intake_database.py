"""A real read-only PostgreSQL source inside the explicitly provisioned test database."""

import secrets
from uuid import uuid4

import pytest
from sqlalchemy import text


@pytest.fixture
def intake_database(pg_databases):
    admin, _ = pg_databases
    suffix = uuid4().hex[:16]
    schema, reader = "intake_" + suffix, "reader_" + suffix
    password = secrets.token_hex(24)
    with admin.engine.begin() as connection:
        connection.execute(text(f"CREATE ROLE {reader} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{password}'"))
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    try:
        with admin.engine.begin() as connection:
            connection.execute(
                text(f"CREATE TABLE {schema}.groups (code TEXT PRIMARY KEY, name TEXT NOT NULL)")
            )
            connection.execute(
                text(
                    f"CREATE TABLE {schema}.items (id BIGINT PRIMARY KEY, name TEXT NOT NULL, group_code TEXT REFERENCES {schema}.groups(code), amount NUMERIC(30,2), occurred_on DATE, enabled BOOLEAN, payload JSONB)"
                )
            )
            connection.execute(
                text(f"INSERT INTO {schema}.groups VALUES ('001','合成分组甲'), ('002','合成分组乙')")
            )
            connection.execute(
                text(
                    f"INSERT INTO {schema}.items VALUES (1,'合成条目甲','001',9007199254740993.20,'2026-01-02',true,'{{\"origin\":\"synthetic\"}}'),(2,'合成条目乙','002',0,NULL,false,NULL)"
                )
            )
            connection.execute(
                text(
                    f"ALTER TABLE {schema}.items ADD COLUMN observed_at TIMESTAMPTZ, ADD COLUMN local_at TIMESTAMP, ADD COLUMN tags TEXT[], ADD COLUMN reference UUID"
                )
            )
            connection.execute(
                text(
                    f"UPDATE {schema}.items SET observed_at='2026-01-02 08:30:00+08', local_at='2026-01-02 08:30:00', tags=ARRAY['甲','乙'], reference='00000000-0000-4000-8000-000000000001' WHERE id=1"
                )
            )
            connection.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO {reader}"))
            connection.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {reader}"))
        url = admin.engine.url
        yield {
            "admin": admin.engine,
            "schema": schema,
            "config": {
                "database": url.database,
                "host": url.host,
                "port": url.port,
                "username": reader,
                "schemas": [schema],
            },
            "credentials": {"password": password},
        }
    finally:
        with admin.engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            connection.execute(text(f"DROP ROLE {reader}"))
