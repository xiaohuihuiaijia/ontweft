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
        limits = ParseLimits.model_validate(request.get("limits") or {})
        if request.get("pdf_batch"):
            from ontology.documents.pdf_native import parse_pdf_batch

            result = parse_pdf_batch(
                Path(request["path"]),
                request["filename"],
                source_id=request["document_id"],
                source_revision=str(request["revision"]),
                content_hash=request["content_hash"],
                start_page=request["pdf_batch"]["start_page"],
                page_limit=request["pdf_batch"]["page_limit"],
                limits=limits,
            )
        else:
            data = Path(request["path"]).read_bytes()
            ir = parse_document(
                data,
                request["filename"],
                source_id=request["document_id"],
                source_revision=str(request["revision"]),
                engine=request["engine"],
                limits=limits,
            )
            result = {"ok": True, "ir": ir.model_dump(mode="json")}
    except DocumentError as exc:
        result = {"ok": False, "code": exc.code, "error": str(exc)}
    Path(args.result).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
