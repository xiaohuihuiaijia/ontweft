"""Local native-PDF checkpoints; jobs store only small batch references and progress."""

import copy
import hashlib
import json
import time
from pathlib import Path

from ontology.core.errors import DomainError, conflict
from ontology.documents.history import check_parse_baseline
from ontology.documents.models import ParseLimits
from ontology.documents.pages import file_digest
from ontology.documents.storage_models import Document
from ontology.jobs.service import check_job_authority, fenced_job

PDF_BATCH_PAGES = 32


def _directory(settings, run):
    root = (settings.data_dir / "temporary").resolve()
    directory = (root / ("pdf-" + run["id"])).resolve()
    if directory.parent != root:
        conflict("解析进度目录不属于当前任务")
    return directory


def _check(session, run):
    job = fenced_job(session, run)
    check_job_authority(session, job)
    check_parse_baseline(session.get(Document, job.payload["document_id"]), job)
    job.lease_until = time.time() + 300
    return job


def parse_pdf_batches(db, settings, run, request, execute):
    path = Path(request["path"])
    limits = ParseLimits.model_validate(request.get("limits") or {})
    if path.stat().st_size > limits.max_bytes:
        raise DomainError(422, "resource_limit", "PDF 超过上传与解析的统一字节限制")
    if file_digest(path) != request["content_hash"]:
        conflict("原件摘要已改变，不能沿用旧解析进度")
    directory = _directory(settings, run)
    directory.mkdir(parents=True, exist_ok=True)
    with db.transaction(run["tenant_id"]) as session:
        job = _check(session, run)
        checkpoint = copy.deepcopy(
            job.checkpoint.get(
                "native_pdf",
                {
                    "batches": [],
                    "pages_completed": 0,
                    "pages_total": None,
                    "characters": 0,
                    "elements": 0,
                },
            )
        )
    result = None
    # Backups intentionally exclude temporary files. Validate retained batches
    # before doing more work, so a restored job never reparses its entire tail
    # only to discover that its earlier evidence is unavailable.
    for record in checkpoint["batches"]:
        batch_path = (directory / record["name"]).resolve()
        if batch_path.parent != directory:
            conflict("解析进度引用不属于当前任务")
        try:
            raw = batch_path.read_bytes()
        except FileNotFoundError:
            conflict("本地解析进度文件已缺失，请重新提交解析")
        if hashlib.sha256(raw).hexdigest() != record["sha256"]:
            conflict("本地解析进度摘要不一致，请重新提交解析")
        batch = json.loads(raw)
        if result is None:
            result = {**batch, "elements": [], "pages": []}
        result["elements"].extend(batch["elements"])
        result["pages"].extend(batch["pages"])
    while checkpoint["pages_total"] is None or checkpoint["pages_completed"] < checkpoint["pages_total"]:
        with db.transaction(run["tenant_id"]) as session:
            _check(session, run)
        start = checkpoint["pages_completed"] + 1
        parsed = execute({**request, "pdf_batch": {"start_page": start, "page_limit": PDF_BATCH_PAGES}})
        if not parsed["ok"]:
            return parsed
        ir = parsed["ir"]
        characters = checkpoint["characters"] + sum(len(element["text"]) for element in ir["elements"])
        elements = checkpoint["elements"] + len(ir["elements"])
        if characters > limits.max_characters or elements > limits.max_elements:
            raise DomainError(422, "resource_limit", "PDF 全文结构超过解析预算，已保留此前页面进度")
        completed = start - 1 + len(ir["pages"])
        if completed < start or (
            checkpoint["pages_total"] is not None and parsed["page_count"] != checkpoint["pages_total"]
        ):
            conflict("PDF 页数与已有进度不一致")
        raw = json.dumps(ir, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        name = f"batch-{run['fencing_token']}-{start}.json"
        record = {"name": name, "sha256": hashlib.sha256(raw).hexdigest()}
        with db.transaction(run["tenant_id"], write=True) as session:
            job = _check(session, run)
            # Write only after the lease check. Different lease tokens never overwrite a batch.
            (directory / name).write_bytes(raw)
            checkpoint = {
                "batches": [*checkpoint["batches"], record],
                "pages_completed": completed,
                "pages_total": parsed["page_count"],
                "characters": characters,
                "elements": elements,
            }
            job.checkpoint = {
                **job.checkpoint,
                "native_pdf": checkpoint,
                "stage": "native_parsing",
                "pages_completed": completed,
                "pages_total": parsed["page_count"],
            }
        if result is None:
            result = {**ir, "elements": [], "pages": []}
        result["elements"].extend(ir["elements"])
        result["pages"].extend(ir["pages"])
    for order, element in enumerate(result["elements"]):
        element["order"] = order
    result["complete"] = len(result["pages"]) == checkpoint["pages_total"] and all(
        page["status"] == "parsed" for page in result["pages"]
    )
    result["quality"] = "basic" if result["complete"] else "needs_review"
    return {"ok": True, "ir": result}


def discard_pdf_batches(settings, run):
    """Called only after the final parse commits; failed/cancelled jobs retain resumable files."""
    directory = _directory(settings, run)
    if directory.exists():
        for path in directory.glob("batch-*.json"):
            if path.resolve().parent == directory:
                path.unlink()
        if not any(directory.iterdir()):
            directory.rmdir()
