"""Seed identity rows against historical schemas without today's ORM columns."""

from uuid import uuid4

from ontology.identity.security import PASSWORDS
from sqlalchemy import MetaData, Table


def bootstrap_historical(db, *, tenant_slug, tenant_name, email, password):
    ids = {key: str(uuid4()) for key in ("tenant_id", "user_id", "project_id")}
    with db.engine.begin() as connection:
        tables = {
            name: Table(name, MetaData(), autoload_with=connection)
            for name in ("tenants", "users", "projects", "memberships")
        }
        connection.execute(
            tables["tenants"].insert().values(id=ids["tenant_id"], slug=tenant_slug, name=tenant_name)
        )
    with db.transaction(ids["tenant_id"]) as session:
        session.execute(
            tables["users"]
            .insert()
            .values(
                id=ids["user_id"],
                tenant_id=ids["tenant_id"],
                email=email.lower(),
                display_name="管理员",
                password_hash=PASSWORDS.hash(password),
                admin=True,
                active=True,
                revision=1,
            )
        )
        session.execute(
            tables["projects"]
            .insert()
            .values(
                id=ids["project_id"],
                tenant_id=ids["tenant_id"],
                name="企业知识项目",
                version=0,
            )
        )
        session.execute(
            tables["memberships"]
            .insert()
            .values(
                id=str(uuid4()),
                tenant_id=ids["tenant_id"],
                project_id=ids["project_id"],
                user_id=ids["user_id"],
                role="owner",
            )
        )
    return ids
