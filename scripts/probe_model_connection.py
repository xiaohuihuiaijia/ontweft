"""Isolated real model smoke test. Key arrives via no-echo prompt, never argv/files."""

import argparse
import getpass
import json
import tempfile
from pathlib import Path

from ontology.core.config import Settings
from ontology.core.errors import DomainError
from ontology.intelligence.client import ModelClient
from ontology.intelligence.connection_api import apply_input, probe
from ontology.intelligence.models import ModelConnection
from ontology.intelligence.schemas import ConnectionInput


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    key = getpass.getpass("Model API key (not echoed): ")
    report = {"scope": "synthetic image/text/schema probe; no private document uploaded", "results": []}
    with tempfile.TemporaryDirectory(prefix="ontweft-model-probe-") as temp:
        settings = Settings(_env_file=None, data_dir=Path(temp))
        row = ModelConnection(id="probe", tenant_id="probe", revision=1)
        apply_input(row, ConnectionInput(name="Probe", base_url=args.base_url, api_key=key), settings)
        key = None
        for role in ("extraction", "synthesis"):
            try:
                result = probe(ModelClient(row, settings), role)
            except DomainError as exc:
                result = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
            report["results"].append({"role": role, **result})
            # Authentication/configuration failures stop; never try old keys or different regions.
            if not result.get("ok"):
                break
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
