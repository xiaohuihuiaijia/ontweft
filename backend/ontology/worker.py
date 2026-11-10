import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from ontology.connectors.base import ConnectorError
from ontology.core.config import Settings
from ontology.core.db import Database
from ontology.core.errors import DomainError, conflict
from ontology.documents.history import (
    capture_current_parse,
    check_parse_baseline,
    retained_parse_state,
    validate_job_configuration,
)
from ontology.documents.storage_models import Document
from ontology.identity.models import Tenant
from ontology.jobs.service import check_job_authority, claim, fenced_job, finish
from ontology.search.content.index import enqueue_content
from ontology.search.content.units import ir_fingerprint, validate_document_ir
from ontology.sources.models import SourceSnapshot

logger = logging.getLogger(__name__)


def parser_environment(temporary_directory: Path | None = None, *, source_environment=None):
    # Parsers receive no database password, source token or application encryption key.
    source = os.environ if source_environment is None else source_environment
    permitted = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    result = {key: value for key, value in source.items() if key.upper() in permitted}
    for key in (
        "ONTOLOGY_DOCLING_PDF_MODE",
        "ONTOLOGY_DOCLING_ARTIFACTS_PATH",
        "ONTOLOGY_DOCLING_MANIFEST_SHA256",
    ):
        if key in source:
            result[key] = source[key]
    result.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if temporary_directory is not None:
        # A parser has an ephemeral home/cache, never the operator's profile.
        isolated = str(temporary_directory.resolve())
        result.update(
            USERPROFILE=isolated,
            HOME=isolated,
            XDG_CACHE_HOME=str(temporary_directory / "cache"),
            HF_HOME=str(temporary_directory / "hf"),
        )
    return result


def process_one(db: Database, settings: Settings, worker_id: str | None = None) -> bool:
    worker_id = worker_id or str(uuid4())
    kinds = ["source.snapshot", "cdc.project", "model.validate", "relation.recompute"]
    if "knowledge" in settings.extensions:
        kinds.append("document.parse")
    run = claim(db, worker_id, kinds=kinds, source_kinds=settings.source_kinds)
    if not run:
        return False
    try:
        if run["kind"] == "relation.recompute":
            from ontology.objects.relation_rules import run_rule

            run_rule(db, run)
            return True
        if run["kind"] == "model.validate":
            from ontology.modeling.validation import run_validation

            run_validation(db, run)
            return True
        if run["kind"] == "source.snapshot":
            from ontology.sources.ingestion import run_snapshot

            run_snapshot(db, settings, run)
            return True
        if run["kind"] == "cdc.project":
            from ontology.cdc.projection import run_projection

            run_projection(db, settings, run)
            return True
        with db.transaction(run["tenant_id"]) as session:
            job = fenced_job(session, run)
            check_job_authority(session, job)
            if job.kind != "document.parse":
                raise DomainError(422, "unsupported_job", "未知任务类型")
            doc = session.get(Document, job.payload["document_id"])
            if (
                not doc
                or doc.tenant_id != job.tenant_id
                or doc.project_id != job.project_id
                or doc.revision != job.payload["revision"]
            ):
                conflict("文档版本或任务范围已改变")
            check_parse_baseline(doc, job)
            validate_job_configuration(job)
            execution_environment = parser_environment()
            request = {
                "path": str((settings.data_dir / "assets" / doc.storage_key).resolve()),
                "filename": doc.filename,
                "document_id": doc.id,
                "revision": doc.revision,
                "engine": job.payload["engine"],
            }
            # Work-in-progress belongs to the Job. An already validated parse
            # stays readable until its replacement commits successfully.
            retained = retained_parse_state(doc)
            doc.state = retained or "processing"
            if retained is None:
                enqueue_content(session, doc, "document")
        settings.data_dir.joinpath("temporary").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="parse-", dir=settings.data_dir / "temporary") as temporary:
            input_path, output_path = Path(temporary) / "request.json", Path(temporary) / "result.json"
            input_path.write_text(json.dumps(request), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-m", "ontology.parse_task", str(input_path), str(output_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=parser_environment(Path(temporary), source_environment=execution_environment),
                timeout=180,
                check=False,
            )
            if completed.returncode != 0 or not output_path.exists():
                raise DomainError(422, "parser_failed", "解析器执行失败，原件已保留")
            parsed = json.loads(output_path.read_text(encoding="utf-8"))

        def apply(session, job):
            doc = session.get(Document, job.payload["document_id"])
            check_parse_baseline(doc, job)
            if parsed["ok"]:
                capture_current_parse(session, doc)
                doc.ir = parsed["ir"]
                # Bind every evidence anchor to the immutable uploaded bytes.
                # A rejected parse is rolled back before the failure is saved.
                validate_document_ir(doc)
                doc.parse_revision += 1
                doc.ir_fingerprint = ir_fingerprint(doc.ir)
                doc.state = "ready" if parsed["ir"].get("complete") else "partial"
                doc.error = None
                capture_current_parse(session, doc, job=job)
            else:
                doc.state, doc.error = retained_parse_state(doc) or "failed", parsed["error"]
            if parsed["ok"] or retained_parse_state(doc) is None:
                enqueue_content(session, doc, "document")

        finish(
            db,
            run,
            result={
                "document_id": request["document_id"],
                "complete": parsed.get("ir", {}).get("complete", False),
            },
            error=None if parsed["ok"] else parsed["error"],
            apply=apply,
        )
    except Exception as exc:  # noqa: BLE001 -- worker boundary must persist a safe failure without driver secrets.
        frames = [
            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
            for frame in traceback.extract_tb(exc.__traceback__, limit=-8)
        ]
        # Do not log exception text, SQL parameters, source values or local
        # variables: driver errors can include credentials and private records.
        logger.error(
            "Job %s (%s) failed: %s at %s", run["id"], run["kind"], type(exc).__name__, " -> ".join(frames)
        )
        message = (
            exc.message
            if isinstance(exc, DomainError)
            else (
                "解析超时" if isinstance(exc, subprocess.TimeoutExpired) else "任务处理失败，请检查运行配置"
            )
        )
        if isinstance(exc, ConnectorError):
            message = f"{exc.code}: {exc}"
        try:
            with db.transaction(run["tenant_id"]) as session:
                job = fenced_job(session, run)
                job.status, job.error, job.lease_until = "failed", message, 0
                if job.kind == "document.parse":
                    doc = session.get(Document, job.payload["document_id"])
                    if doc and doc.tenant_id == job.tenant_id and doc.project_id == job.project_id:
                        try:
                            check_parse_baseline(doc, job)
                        except DomainError:
                            # A different Job may have committed a newer parse.
                            # Mark this Job failed without touching that version.
                            pass
                        else:
                            retained = retained_parse_state(doc)
                            doc.state, doc.error = retained or "failed", message
                            if retained is None:
                                enqueue_content(session, doc, "document")
                elif job.kind == "source.snapshot":
                    snapshot = session.get(SourceSnapshot, job.payload["snapshot_id"])
                    if snapshot:
                        snapshot.status, snapshot.error = "failed", message
                elif job.kind == "cdc.project":
                    from ontology.cdc.projection import record_failure

                    record_failure(session, job, exc, message)
        except DomainError:
            logger.info("Discarded stale job result for %s", run["id"])
    return True


def run_worker(settings: Settings, once=False, action_registry=None):
    db = Database(settings)
    worker_id = str(uuid4())
    registry = action_registry or {}
    try:
        while True:
            (settings.data_dir / "worker.heartbeat").touch()
            from ontology.actions.runtime import process_one_action

            worked = False
            with db.transaction() as session:
                tenants = list(session.scalars(select(Tenant.id)))
            for tenant_id in tenants:
                from ontology.cdc.projection import schedule_pending

                schedule_pending(db, tenant_id)
                if "automation" in settings.extensions and process_one_action(
                    db, tenant_id, registry, worker_id
                ):
                    worked = True
                    break
            if not worked:
                worked = process_one(db, settings, worker_id)
            from ontology.search.objects.index import process_one_index

            # Index maintenance gets an independent bounded turn, including
            # during a continuous ingestion/action workload.
            for tenant_id in tenants:
                if process_one_index(db, tenant_id):
                    worked = True
                    break
            from ontology.search.content.index import process_one_content_index

            for tenant_id in tenants if "knowledge" in settings.extensions else ():
                if process_one_content_index(db, tenant_id):
                    worked = True
                    break
            if once:
                return
            if not worked:
                time.sleep(1)
    finally:
        db.engine.dispose()
