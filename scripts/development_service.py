"""Run the local app or worker with the dedicated non-owner PostgreSQL role."""

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=["api", "worker"])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    os.environ["ONTOLOGY_DATABASE_URL"] = (root / "var/runtime-database-url.txt").read_text().strip()
    login_file = root / "var/development-login.json"
    if login_file.is_file():
        os.environ.setdefault(
            "ONTOLOGY_LOGIN_TENANT_SLUG",
            json.loads(login_file.read_text(encoding="utf-8-sig"))["tenant_slug"],
        )
    if args.service == "api":
        import uvicorn

        uvicorn.run("ontology.api:create_app", factory=True, host="127.0.0.1", port=8000)
    else:
        from ontology.core.config import Settings
        from ontology.worker import run_worker

        run_worker(Settings())


if __name__ == "__main__":
    main()
