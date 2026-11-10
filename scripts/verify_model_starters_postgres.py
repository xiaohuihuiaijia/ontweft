"""Check starter contracts on an isolated PostgreSQL container; no saved credentials."""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "var/reports/open-source-readiness-20261109"


def main():
    parser = argparse.ArgumentParser(description="Isolated PostgreSQL starter and optional graph contracts")
    parser.add_argument("--include-graph", action="store_true")
    parser.add_argument("--include-documents", action="store_true")
    args = parser.parse_args()
    prefix = "documents-postgres" if args.include_documents else "graph-postgres" if args.include_graph else "starter-postgres"
    tests = ["backend/tests/modeling/test_templates.py"]
    if args.include_graph:
        tests.append("backend/tests/objects/test_graph_scale.py")
    if args.include_documents:
        tests.append("backend/tests/documents/test_document_reparse.py")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    name = "ontweft-starter-pg-" + uuid4().hex
    admin_password, runtime_password = secrets.token_hex(24), secrets.token_hex(24)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("ONTOLOGY_")}
    docker_env = {**environment, "POSTGRES_PASSWORD": admin_password}
    result = {"passed": False, "isolated_container": name, "uses_developer_credentials": False}

    def docker(*args, input=None, check=True):
        return subprocess.run(
            ["docker", *args],
            cwd=ROOT,
            env=docker_env,
            input=input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
            timeout=60,
        )

    started = False
    try:
        docker(
            "run",
            "--detach",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--memory=512m",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            "POSTGRES_PASSWORD",
            "postgres:17",
        )
        started = True
        for _ in range(60):
            # Initialization temporarily accepts Unix sockets before the final TCP server starts.
            if docker("exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", check=False).returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Isolated PostgreSQL did not become ready")
        port = docker("port", name, "5432/tcp").stdout.strip().rsplit(":", 1)[1]
        docker(
            "exec",
            "-i",
            name,
            "psql",
            "-U",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            input=(
                "CREATE ROLE ontweft_test_runtime LOGIN NOSUPERUSER NOBYPASSRLS "
                f"PASSWORD '{runtime_password}';\n"
            ),
        )
        result["postgres_version"] = docker(
            "exec", name, "psql", "-U", "postgres", "-Atc", "SHOW server_version"
        ).stdout.strip()
        environment.update(
            {
                "PYTHONUTF8": "1",
                "ONTOLOGY_TEST_ADMIN_URL": f"postgresql+pg8000://postgres:{admin_password}@127.0.0.1:{port}/postgres",
                "ONTOLOGY_TEST_RUNTIME_URL": f"postgresql+pg8000://ontweft_test_runtime:{runtime_password}@127.0.0.1:{port}/postgres",
            }
        )
        with (OUTPUT / f"{prefix}.log").open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/test_backend.py",
                    "--postgres",
                    *tests,
                    "-q",
                    f"--junitxml=var/reports/open-source-readiness-20261109/{prefix}.xml",
                ],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=600,
                check=False,
            )
        result["exit_code"] = completed.returncode
        result["passed"] = completed.returncode == 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        # Avoid logging environments, SQL inputs or credential-bearing connection URLs.
        result["error"] = type(error).__name__
        if isinstance(error, subprocess.CalledProcessError):
            result["exit_code"] = error.returncode
            result["diagnostic"] = (error.stderr or "").replace(admin_password, "[redacted]").replace(runtime_password, "[redacted]")[:2000]
    finally:
        if started:
            cleanup = docker("rm", "--force", name, check=False)
            result["container_removed"] = cleanup.returncode == 0
        (OUTPUT / f"{prefix}-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
