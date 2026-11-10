"""Backup the Compose deployment; restore only to an empty database and asset volume.

Run with the same COMPOSE_PROJECT_NAME and environment as the intended deployment.
Credentials stay in Compose's environment, never in the backup manifest.
"""

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path


def compose(*args, **kwargs):
    return subprocess.run(["docker", "compose", *args], check=True, **kwargs)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def database(*args, **kwargs):
    return compose("exec", "-T", "db", *args, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["create", "restore"])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    target = args.directory.resolve()
    started = time.monotonic()
    if args.operation == "create":
        target.mkdir(parents=True, exist_ok=False)
        # DB first: file/version originals are append-only. Files created during
        # this dump may be extra, but cannot remove a referenced original.
        with (target / "database.dump").open("wb") as out:
            database(
                "pg_dump",
                "-U",
                "ontweft_owner",
                "-d",
                "ontweft",
                "-Fc",
                "--no-owner",
                "--no-privileges",
                stdout=out,
            )
        with (target / "assets.tar").open("wb") as out:
            compose(
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "tar",
                "api",
                "-C",
                "/data",
                "-cf",
                "-",
                ".",
                stdout=out,
            )
        manifest = {
            "format": 1,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "files": {name: digest(target / name) for name in ("database.dump", "assets.tar")},
            "seconds": round(time.monotonic() - started, 2),
            "image": os.environ.get("ONTWEFT_IMAGE", "ontweft:local"),
            "encryption_key": "Retain the deployment key separately; not included",
        }
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"Backup completed in {manifest['seconds']} seconds: {target}")
        return
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    if manifest["format"] != 1 or any(
        digest(target / name) != manifest["files"][name] for name in ("database.dump", "assets.tar")
    ):
        raise SystemExit("Backup checksums do not match; nothing restored")
    tables = database(
        "psql",
        "-U",
        "ontweft_owner",
        "-d",
        "ontweft",
        "-Atc",
        "SELECT count(*) FROM pg_tables WHERE schemaname='public'",
        capture_output=True,
        text=True,
    ).stdout.strip()
    if tables != "0":
        raise SystemExit("Restore requires an empty database in a separate Compose project")
    compose(
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "python",
        "api",
        "-c",
        "from pathlib import Path; assert not any(Path('/data').iterdir()), 'Restore requires an empty asset volume'",
    )
    # These are only the explicitly selected deployment's services.
    compose("stop", "api", "worker")
    with (target / "database.dump").open("rb") as source:
        database(
            "pg_restore",
            "-U",
            "ontweft_owner",
            "-d",
            "ontweft",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            stdin=source,
        )
    with (target / "assets.tar").open("rb") as source:
        compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "tar",
            "api",
            "-C",
            "/data",
            "-xf",
            "-",
            stdin=source,
        )
    # Regrant the restricted runtime role using the destination password.
    compose("run", "--rm", "--no-deps", "migrate")
    print(
        f"Restored in {time.monotonic() - started:.2f} seconds. Start api and worker after checking the retained encryption key and origin."
    )


if __name__ == "__main__":
    main()
