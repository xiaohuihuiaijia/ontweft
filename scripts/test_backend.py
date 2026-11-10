"""Run repeatable backend checks, optionally including isolated PostgreSQL tests."""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres", action="store_true", help="Load local test credentials; never print them"
    )
    args, pytest_args = parser.parse_known_args()
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONUTF8": "1"}
    if args.postgres:
        for variable, filename in (
            ("ONTOLOGY_TEST_ADMIN_URL", "database-url.txt"),
            ("ONTOLOGY_TEST_RUNTIME_URL", "runtime-database-url.txt"),
        ):
            if not environment.get(variable):
                path = root / "var" / filename
                if not path.is_file():
                    parser.error(f"Set {variable} or provide the local credential file {filename}")
                environment[variable] = path.read_text(encoding="utf-8").strip()
    return subprocess.run(
        [sys.executable, "-m", "pytest", *(pytest_args or ["backend/tests", "-q"])],
        cwd=root,
        env=environment,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
