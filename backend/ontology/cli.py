import argparse
import getpass

from pydantic import TypeAdapter
from sqlalchemy import select, text

from ontology.core.config import Settings
from ontology.core.db import Database
from ontology.identity.models import Membership, Project, Tenant, User
from ontology.identity.schemas import Username
from ontology.identity.security import PASSWORDS


def bootstrap(
    db: Database,
    *,
    tenant_slug: str,
    tenant_name: str,
    email: str,
    password: str,
    name="管理员",
    username: str | None = None,
):
    if len(password) < 12:
        raise ValueError("密码至少 12 个字符")
    username = TypeAdapter(Username).validate_python(username) if username is not None else None
    db.create_schema()
    with db.transaction() as session:
        if session.scalar(select(Tenant).where(Tenant.slug == tenant_slug)):
            raise ValueError("企业已存在，初始化不会覆盖已有账号")
        tenant = Tenant(slug=tenant_slug, name=tenant_name)
        session.add(tenant)
        session.flush()
        if db.engine.dialect.name == "postgresql":
            session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant.id})
        user = User(
            tenant_id=tenant.id,
            email=email.lower(),
            username=username,
            display_name=name,
            password_hash=PASSWORDS.hash(password),
            admin=True,
        )
        project = Project(tenant_id=tenant.id, name="企业知识项目")
        session.add_all([user, project])
        session.flush()
        session.add(Membership(tenant_id=tenant.id, project_id=project.id, user_id=user.id, role="owner"))
        return {"tenant_id": tenant.id, "user_id": user.id, "project_id": project.id}


def main():
    parser = argparse.ArgumentParser(description="企业本体平台管理")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--tenant", required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--email", required=True)
    init.add_argument("--username", help="管理员登录用户名（3–64 位字母、数字、点、下划线或短横线）")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    work = sub.add_parser("worker")
    work.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        db = Database(Settings())
        result = bootstrap(
            db,
            tenant_slug=args.tenant,
            tenant_name=args.name,
            email=args.email,
            username=args.username,
            password=getpass.getpass("管理员密码（至少12位）: "),
        )
        print(f"企业与首个项目已创建：{result['project_id']}")
    elif args.command == "serve":
        import uvicorn

        uvicorn.run("ontology.api:create_app", factory=True, host=args.host, port=args.port)
    else:
        from ontology.worker import run_worker

        run_worker(Settings(), once=args.once)


if __name__ == "__main__":
    main()
