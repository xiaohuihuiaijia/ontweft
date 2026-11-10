"""Separate parser process. A production container boundary is configured separately."""

import argparse
import json
from pathlib import Path

from ontology.documents import DocumentError, ParseLimits, parse_document


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    parser.add_argument("result")
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    try:
        data = Path(request["path"]).read_bytes()
        ir = parse_document(
            data,
            request["filename"],
            source_id=request["document_id"],
            source_revision=str(request["revision"]),
            engine=request["engine"],
            limits=ParseLimits(),
        )
        result = {"ok": True, "ir": ir.model_dump(mode="json")}
    except DocumentError as exc:
        result = {"ok": False, "code": exc.code, "error": str(exc)}
    Path(args.result).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
