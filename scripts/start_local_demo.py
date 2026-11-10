"""Run a persistent, loopback-only learning workspace without developer credentials."""

import argparse
import getpass
import json
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


def demo_settings(data: Path, port: int) -> Settings:
    # Explicit values take precedence over ambient ONTOLOGY_* variables; .env is never read.
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
        allowed_source_hosts=[],
        allow_private_source_addresses=False,
        max_upload_bytes=25 * 1024 * 1024,
        feishu_frontend_origin=f"http://127.0.0.1:{port}",
    )


def create_demo_app(data: Path, *, port: int = 8020, password: str | None = None):
    data = data.resolve()
    existing = inspect_directory(data)
    if not existing and (password is None or len(password) < 12):
        raise ValueError("首次启动需要至少 12 个字符的密码。")
    assets = ROOT / "frontend" / "dist"
    if not (assets / "index.html").is_file():
        raise ValueError(
            "缺少前端构建，请先执行 npm --prefix frontend ci 和 npm --prefix frontend run build。"
        )
    app = create_app(demo_settings(data, port))
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
    parser.add_argument("command", nargs="?", choices=["serve", "worker"], default="serve")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "var" / "local-demo")
    parser.add_argument("--port", type=int, default=8020)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间")
    data = args.data_dir.resolve()
    try:
        existing = inspect_directory(data)
        if args.command == "worker":
            if not existing:
                parser.error("请先启动一次 serve，创建体验工作空间。")
            from ontology.worker import run_worker

            run_worker(demo_settings(data, args.port))
            return
        password = None
        if not existing:
            print("创建本机体验账号 owner；这是独立 SQLite 工作空间，不连接现有开发数据库。")
            password = getpass.getpass("设置密码（至少 12 个字符，不回显）: ")
            if password != getpass.getpass("再次输入密码: "):
                parser.error("两次密码不同，尚未创建工作空间。")
        app = create_demo_app(data, port=args.port, password=password)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"打开 http://127.0.0.1:{args.port}，用户名 owner。数据保存在 {data}")
    print("仅用于本机学习；生产部署与 PostgreSQL/RLS 验证请按 README 另行配置。")
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
