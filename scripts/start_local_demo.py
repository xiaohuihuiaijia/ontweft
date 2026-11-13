"""Run a persistent, loopback-only learning workspace without developer credentials."""

import argparse
import getpass
import ipaddress
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
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
        proposal_columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_proposals)")}
    return (bool(columns) and ("previous_stream_id" not in columns or "ix_entity_browse" not in indexes)) or (
        bool(proposal_columns) and "model_notes" not in proposal_columns
    )


def upgrade_demo_schema(data: Path):
    """Apply known local SQLite upgrades offline, retaining a real database backup."""
    if not inspect_directory(data):
        raise ValueError("体验目录尚未创建")
    if not needs_schema_upgrade(data):
        return None
    import importlib.util
    from uuid import uuid4

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    backup = data / ("demo-before-upgrade-" + uuid4().hex[:8] + ".db")
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
                    proposal_columns = {
                        row[1] for row in connection.exec_driver_sql("PRAGMA table_info(ai_proposals)")
                    }
                    required = (
                        [] if "previous_stream_id" in columns else ["ef311029a600_cdc_generations.py"]
                    ) + ([] if "ix_entity_browse" in indexes else ["ef311029a700_entity_browse.py"])
                    if proposal_columns and "model_notes" not in proposal_columns:
                        required.append("ef311113a200_ai_model_notes.py")
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
        max_upload_bytes=256 * 1024 * 1024,
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


def new_password() -> str:
    print("创建本机体验账号 owner；这是独立 SQLite 工作空间，不连接现有开发数据库。")
    password = getpass.getpass("设置密码（至少 12 个字符，不回显）: ")
    if password != getpass.getpass("再次输入密码: "):
        raise ValueError("两次密码不同，尚未创建工作空间。")
    if len(password) < 12:
        raise ValueError("密码至少 12 个字符。")
    return password


def stop_experience_children(children):
    """Stop only process trees started by this experience invocation."""
    for child in reversed(children):
        if child.poll() is not None:
            continue
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
        else:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                child.kill()
            else:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait()


def check_experience_children(children):
    for name, child in zip(("API", "worker"), children, strict=True):
        code = child.poll()
        if code is not None:
            raise RuntimeError(f"体验 {name} 已退出（状态 {code}）；本次启动已停止，数据保留。")


def interrupt_experience(_signum, _frame):
    raise KeyboardInterrupt


def check_experience_port(port: int, container: bool):
    try:
        with socket.socket() as listener:
            listener.bind(("0.0.0.0" if container else "127.0.0.1", port))
    except OSError:
        raise ValueError(f"无法使用端口 {port}；请停止原服务或用 --port 选择其他端口。") from None


def run_experience(
    data: Path,
    *,
    port: int = 8020,
    container: bool = False,
    source_hosts: list[str] | None = None,
    allow_private_sources: bool = False,
):
    check_experience_port(port, container)
    from experience_sample import ensure_sample, read_sample_state

    existing = inspect_directory(data)
    sample = read_sample_state(data) if existing else None
    complete = bool(sample and sample.get("status") == "complete")
    password = new_password() if not existing else None
    if existing and not complete:
        password = getpass.getpass("输入当前 owner 密码以继续准备样例（不回显）: ")
    app = create_demo_app(
        data,
        port=port,
        password=password,
        source_hosts=source_hosts,
        allow_private_sources=allow_private_sources,
    )
    app.state.db.engine.dispose()

    common = ["--data-dir", str(data), "--port", str(port)]
    for host in source_hosts or []:
        common.extend(["--source-host", host])
    if allow_private_sources:
        common.append("--allow-private-sources")
    commands = [
        ["serve", *common, *(["--container"] if container else [])],
        ["worker", *common],
    ]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("ONTOLOGY_")}
    environment.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    process_options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    )
    children = []
    previous_term = signal.signal(signal.SIGTERM, interrupt_experience)
    print("正在启动 API 和 worker；首次启动将通过实际接口准备合成样例。", flush=True)
    try:
        for arguments in commands:
            children.append(
                subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), *arguments],
                    cwd=ROOT,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    **process_options,
                )
            )
        with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/", timeout=10, trust_env=False) as client:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                check_experience_children(children)
                try:
                    if client.get("health", timeout=1).is_success:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError("体验 API 未在启动时限内就绪；本次启动已停止，数据保留。")
            if not complete:
                login = client.post("auth/login", json={"username": "owner", "password": password})
                if login.status_code == 401:
                    raise ValueError("owner 密码不正确；账号未重置，请使用原密码重新启动 experience。")
                login.raise_for_status()
                sample = ensure_sample(client, data)
                client.post("auth/logout").raise_for_status()
            check_experience_children(children)
        query = urlencode({"project": sample["project_id"]})
        print(f"体验已就绪：http://127.0.0.1:{port}/#graph?{query}，使用 owner 登录。", flush=True)
        print(
            "已有体验数据已保留，不会重建或覆盖你对样例的修改。"
            if complete
            else "“产品与库存 · 合成样例”已初始化，可查看属性、关系条件与原文依据。",
            flush=True,
        )
        while True:
            check_experience_children(children)
            time.sleep(0.5)
    except httpx.HTTPError:
        raise RuntimeError("体验初始化的本机 API 请求失败；本次启动已停止，数据保留。") from None
    finally:
        try:
            stop_experience_children(children)
        finally:
            signal.signal(signal.SIGTERM, previous_term)


def main():
    parser = argparse.ArgumentParser(description="序枢本机体验：SQLite 持久保存，仅监听 127.0.0.1")
    parser.add_argument(
        "command", nargs="?", choices=["serve", "worker", "migrate", "experience"], default="serve"
    )
    parser.add_argument("--data-dir", type=Path, default=ROOT / "var" / "local-demo")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument(
        "--container", action="store_true", help="容器内部监听 0.0.0.0；宿主端口请仅映射到 127.0.0.1"
    )
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
        if args.command == "experience":
            run_experience(
                data,
                port=args.port,
                container=args.container,
                source_hosts=args.source_host,
                allow_private_sources=args.allow_private_sources,
            )
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
            password = new_password()
        app = create_demo_app(
            data,
            port=args.port,
            password=password,
            source_hosts=args.source_host,
            allow_private_sources=args.allow_private_sources,
        )
    except KeyboardInterrupt:
        print("体验已停止，已有数据保留。")
        return
    except (ValueError, OSError, RuntimeError) as error:
        parser.error(str(error))
    print(f"打开 http://127.0.0.1:{args.port}，用户名 owner。数据保存在 {data}")
    print("仅用于本机学习；生产部署与 PostgreSQL/RLS 验证请按 README 另行配置。")
    uvicorn.run(app, host="0.0.0.0" if args.container else "127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
