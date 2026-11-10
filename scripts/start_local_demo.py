"""Run a persistent, loopback-only learning workspace without developer credentials."""

import argparse
import getpass
import ipaddress
import json
import re
import sqlite3
from pathlib import Path

import uvicorn
from fastapi.staticfiles import StaticFiles
from ontology.api import create_app
from ontology.cli import bootstrap
from ontology.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
MARKER = ".ontweft-local-demo.json"
PROFILE = {"format_version": 1, "tenant_slug": "local-demo"}


def inspect_directory(data: Path) -> bool:
    """Return whether this is our existing workspace; never adopt an unrelated database."""
    marker = data / MARKER
    if marker.is_file():
        if json.loads(marker.read_text(encoding="utf-8")) != PROFILE or not (data / "demo.db").is_file():
            raise ValueError("体验目录标识或数据库不完整；请保留原文件，使用另一个空目录。")
        return True
    if data.exists() and any(data.iterdir()):
        raise ValueError("目标目录已有其他文件；请选择空目录，避免修改现有数据库。")
    return False


def needs_schema_upgrade(data: Path) -> bool:
    with sqlite3.connect(data / "demo.db") as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(cdc_streams)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(entities)")}
    return bool(columns) and ("previous_stream_id" not in columns or "ix_entity_browse" not in indexes)


def upgrade_demo_schema(data: Path):
    """One offline upgrade for pre-generation local workspaces, with a real SQLite backup."""
    if not inspect_directory(data):
        raise ValueError("体验目录尚未创建")
    if not needs_schema_upgrade(data):
        return None
    import importlib.util
    from uuid import uuid4

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    backup = data / ("demo-before-generations-" + uuid4().hex[:8] + ".db")
    with sqlite3.connect(data / "demo.db") as source, sqlite3.connect(backup) as target:
        source.backup(target)
    engine = create_engine(f"sqlite:///{(data / 'demo.db').as_posix()}")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN EXCLUSIVE")
            try:
                with Operations.context(MigrationContext.configure(connection)):
                    columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(cdc_streams)")}
                    indexes = {row[1] for row in connection.exec_driver_sql("PRAGMA index_list(entities)")}
                    required = (
                        [] if "previous_stream_id" in columns else ["ef311029a600_cdc_generations.py"]
                    ) + ([] if "ix_entity_browse" in indexes else ["ef311029a700_entity_browse.py"])
                    for filename in required:
                        spec = importlib.util.spec_from_file_location(
                            "local_schema_upgrade", ROOT / "backend/migrations/versions" / filename
                        )
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        module.upgrade()
                if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                    raise ValueError("升级后的外键检查失败，原事务已回滚")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    finally:
        engine.dispose()
    return backup


def source_allowlist(values: list[str] | None) -> list[str]:
    hosts = []
    for value in values or []:
        host = value.strip().rstrip(".").lower()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", host
            ):
                raise ValueError("来源主机须为明确的主机名或 IP，不含协议、端口、路径或通配符。") from None
        if host not in hosts:
            hosts.append(host)
    return hosts


def demo_settings(
    data: Path, port: int, *, source_hosts: list[str] | None = None, allow_private_sources: bool = False
) -> Settings:
    # Explicit values take precedence over ambient ONTOLOGY_* variables; .env is never read.
    hosts = source_allowlist(source_hosts)
    if allow_private_sources and not hosts:
        raise ValueError("放行内网来源时还须用 --source-host 指定主机，不支持全局放行。")
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{(data / 'demo.db').as_posix()}",
        data_dir=data,
        allowed_origins=[f"http://127.0.0.1:{port}"],
        session_hours=12,
        login_tenant_slug="local-demo",
        secure_cookies=False,
        environment="development",
        encryption_key="",
        allowed_source_hosts=hosts,
        allow_private_source_addresses=allow_private_sources,
        max_upload_bytes=25 * 1024 * 1024,
        feishu_frontend_origin=f"http://127.0.0.1:{port}",
        extensions=[],
        model_approval="single",
    )


def create_demo_app(
    data: Path,
    *,
    port: int = 8020,
    password: str | None = None,
    source_hosts: list[str] | None = None,
    allow_private_sources: bool = False,
):
    data = data.resolve()
    existing = inspect_directory(data)
    if existing and needs_schema_upgrade(data):
        raise ValueError(
            "请停止体验 API/worker 后先运行 start_local_demo.py migrate --data-dir <原目录>；升级会保留数据库备份。"
        )
    if not existing and (password is None or len(password) < 12):
        raise ValueError("首次启动需要至少 12 个字符的密码。")
    assets = ROOT / "frontend" / "dist"
    if not (assets / "index.html").is_file():
        raise ValueError(
            "缺少前端构建，请先执行 npm --prefix frontend ci 和 npm --prefix frontend run build。"
        )
    app = create_app(
        demo_settings(data, port, source_hosts=source_hosts, allow_private_sources=allow_private_sources)
    )
    if not existing:
        bootstrap(
            app.state.db,
            tenant_slug="local-demo",
            tenant_name="我的本体工作空间 · 本机体验",
            email="owner@example.test",
            username="owner",
            password=password,
        )
        (data / MARKER).write_text(json.dumps(PROFILE), encoding="utf-8")
    app.mount("/", StaticFiles(directory=assets, html=True), name="local-demo")
    return app


def main():
    parser = argparse.ArgumentParser(description="序枢本机体验：SQLite 持久保存，仅监听 127.0.0.1")
    parser.add_argument("command", nargs="?", choices=["serve", "worker", "migrate"], default="serve")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "var" / "local-demo")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument(
        "--source-host",
        action="append",
        default=[],
        metavar="HOST",
        help="允许连接的来源主机名/IP；可重复，不含协议和端口",
    )
    parser.add_argument(
        "--allow-private-sources",
        action="store_true",
        help="允许名单中的主机解析到内网或回环地址，仍只监听本机",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间")
    data = args.data_dir.resolve()
    try:
        # Reject malformed allowlists before prompting or creating a workspace.
        settings = demo_settings(
            data, args.port, source_hosts=args.source_host, allow_private_sources=args.allow_private_sources
        )
        existing = inspect_directory(data)
        if args.command == "migrate":
            backup = upgrade_demo_schema(data)
            print(f"本机数据库已升级，原库备份：{backup}" if backup else "本机数据库无需此项升级。")
            return
        if args.command == "worker":
            if not existing:
                parser.error("请先启动一次 serve，创建体验工作空间。")
            if needs_schema_upgrade(data):
                raise ValueError("请先停止体验 API/worker 并运行 migrate 升级原目录，再启动 worker。")
            from ontology.worker import run_worker

            run_worker(settings)
            return
        password = None
        if not existing:
            print("创建本机体验账号 owner；这是独立 SQLite 工作空间，不连接现有开发数据库。")
            password = getpass.getpass("设置密码（至少 12 个字符，不回显）: ")
            if password != getpass.getpass("再次输入密码: "):
                parser.error("两次密码不同，尚未创建工作空间。")
        app = create_demo_app(
            data,
            port=args.port,
            password=password,
            source_hosts=args.source_host,
            allow_private_sources=args.allow_private_sources,
        )
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"打开 http://127.0.0.1:{args.port}，用户名 owner。数据保存在 {data}")
    print("仅用于本机学习；生产部署与 PostgreSQL/RLS 验证请按 README 另行配置。")
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
