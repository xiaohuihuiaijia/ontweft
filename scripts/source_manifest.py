"""Refresh or check the tracked source inventory; this is not a licensing approval."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "SOURCE_MANIFEST.json"
FORBIDDEN = {"var", ".venv", "node_modules", "dist", "target", "output", "reports", "__pycache__"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    old = json.loads(MANIFEST.read_text(encoding="utf-8"))
    origins = {item["path"]: item for item in old["files"]}
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
    files = []
    for relative in sorted(filter(None, paths)):
        if relative == "SOURCE_MANIFEST.json":
            continue
        path = ROOT / relative
        if any(part in FORBIDDEN for part in Path(relative).parts) or path.name.startswith(".env"):
            raise SystemExit(f"Runtime or secret configuration path cannot be inventoried: {relative}")
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise SystemExit(f"Source path escapes repository: {relative}")
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        prior = origins.get(relative, {})
        origin = prior.get("source") if digest == prior.get("sha256") else "maintained-in-public-repository"
        files.append({"path": relative, "bytes": len(content), "sha256": digest, "source": origin})
    if args.check:
        if files != old["files"]:
            raise SystemExit("Tracked source differs from SOURCE_MANIFEST.json; review and refresh it")
        print(f"Source inventory matches {len(files)} tracked files")
    else:
        old["files"] = files
        MANIFEST.write_text(json.dumps(old, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Updated {len(files)} source records; no credentials or licensing approval inferred")


if __name__ == "__main__":
    main()
