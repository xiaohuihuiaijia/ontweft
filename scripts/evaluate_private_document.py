"""Explicit local-only evaluation; prints counts, never material or model output.

Run with an authorized PDF and an ignored output directory. A no-echo prompt
supplies the API key. Only the configured Bailian endpoint receives material.
Accuracy requires independent local annotations; model agreement is not truth.
"""

import argparse
import getpass
import json
import logging
import secrets
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient
from ontology.api import create_app
from ontology.cli import bootstrap
from ontology.core.config import Settings
from ontology.documents.storage_models import Document
from ontology.identity.models import Project, Tenant, User
from ontology.identity.security import PASSWORDS
from ontology.intelligence.models import ModelConnection
from ontology.intelligence.proposal_models import AIProposal
from ontology.jobs.models import Job
from ontology.worker import process_one
from sqlalchemy import select


def retry_budget_cny(previous_budget, total_budget, checkpoints, own_checkpoint):
    """Only shrink a retry's cap; unresolved reservations still consume the total."""
    remaining = total_budget - sum(
        cp.get("cost_cny", 0) + cp.get("reserved_cny", 0) for cp in checkpoints
    )
    return min(
        previous_budget,
        remaining + own_checkpoint.get("cost_cny", 0) + own_checkpoint.get("reserved_cny", 0),
    )


def matches_evaluation(inputs, document_id, pages, quality, goal):
    """Resume only the same source and explicit page selection, keeping its ledger."""
    saved_pages = inputs.get("pages", {}).get(document_id)
    return (
        inputs.get("quality") == quality
        and inputs.get("instructions") == goal
        and [item["id"] for item in inputs.get("documents", [])] == [document_id]
        and not inputs.get("sources")
        and saved_pages is not None
        and set(saved_pages) == set(pages)
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf")
    parser.add_argument("--output", required=True)
    parser.add_argument("--scenario", choices=("all", "balanced", "high", "custom"), default="all")
    parser.add_argument(
        "--resume", action="store_true", help="Resume this isolated evaluation and retain its spending"
    )
    parser.add_argument("--pages", default="10,11,50,51,100,101,200,201,300,301,400,401")
    parser.add_argument("--budget", type=float, default=19.97)
    parser.add_argument("--base-url", default="https://dashscope.aliyuncs.com/compatible-mode/v1")
    args = parser.parse_args()
    from urllib.parse import urlsplit

    if urlsplit(args.base_url).hostname != "dashscope.aliyuncs.com":
        raise SystemExit("This private test authorizes only the configured mainland Bailian endpoint")
    root = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()
    if not output.is_relative_to(root / "var") or (output.exists() != args.resume):
        raise SystemExit("Use a new ignored var/ directory, or --resume its existing isolated evaluation")
    if not args.resume and not args.pdf:
        raise SystemExit("--pdf is required for a new evaluation")
    key = None if args.resume else getpass.getpass("Bailian API key (not echoed): ")
    output.mkdir(parents=True, exist_ok=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings(
        _env_file=None, database_url="sqlite:///" + str(output / "evaluation.db"), data_dir=output / "data"
    )
    app = create_app(settings)
    password = secrets.token_urlsafe(28)
    if args.resume:
        report = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        with app.state.db.transaction(None, write=True) as session:
            tenant = session.scalar(select(Tenant).where(Tenant.slug == "private-evaluation"))
            user = session.scalar(
                select(User).where(User.tenant_id == tenant.id, User.email == "evaluation@example.invalid")
            )
            project = session.scalar(select(Project).where(Project.tenant_id == tenant.id))
            user.password_hash = PASSWORDS.hash(password)
            ids = {"tenant_id": tenant.id, "project_id": project.id}
    else:
        ids = bootstrap(
            app.state.db,
            tenant_slug="private-evaluation",
            tenant_name="Local evaluation",
            email="evaluation@example.invalid",
            password=password,
        )
        report = {
            "scope": "private local document evaluation",
            "budget_cny": args.budget,
            "accuracy_measured": False,
            "gold_annotations_reviewed": 0,
            "runs": [],
        }
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={
                "tenant_slug": "private-evaluation",
                "email": "evaluation@example.invalid",
                "password": password,
            },
        )
        response.raise_for_status()
        if args.resume:
            with app.state.db.transaction(ids["tenant_id"]) as session:
                row = session.scalar(select(ModelConnection))
                if row.base_url != args.base_url:
                    raise SystemExit("The saved evaluation endpoint differs; resume cannot switch providers")
                connection = {"id": row.id}
                document = {"id": session.scalar(select(Document.id))}
        else:
            response = client.post(
                "/api/model-connections",
                json={"name": "Authorized Bailian evaluation", "base_url": args.base_url, "api_key": key},
            )
            key = None
            response.raise_for_status()
            connection = response.json()
            with open(args.pdf, "rb") as stream:
                response = client.post(
                    "/api/documents/upload",
                    data={"project_id": ids["project_id"]},
                    files={"file": ("private-evaluation.pdf", stream, "application/pdf")},
                )
            response.raise_for_status()
            document = response.json()["document"]
        selected = [int(p) for p in args.pages.split(",")]
        scenarios = [
            ("balanced", "提取产品系列、型号、规格、单位、适用条件和有明确证据的选型及配套关系"),
            ("high", "提取产品系列、型号、规格、单位、适用条件和有明确证据的选型及配套关系"),
            ("balanced", "仅关注产品选型中的电气规格、数值范围、单位与适用条件，不提取无关外观描述"),
        ]

        def remaining_budget():
            with app.state.db.transaction(ids["tenant_id"]) as session:
                checkpoints = session.scalars(select(Job.checkpoint).where(Job.kind == "ai.propose")).all()
            return report["budget_cny"] - sum(
                cp.get("cost_cny", 0) + cp.get("reserved_cny", 0) for cp in checkpoints
            )

        remaining = remaining_budget()
        for scenario, (quality, goal) in zip(("balanced", "high", "custom"), scenarios, strict=True):
            if args.scenario not in {"all", scenario}:
                continue
            if remaining < 0.1:
                break
            with app.state.db.transaction(ids["tenant_id"]) as session:
                previous = next(
                    (
                        row
                        for row in session.scalars(select(AIProposal))
                        if matches_evaluation(row.inputs, document["id"], selected, quality, goal)
                    ),
                    None,
                )
            if previous:
                response = client.get(f"/api/ai/proposals/{previous.id}")
                response.raise_for_status()
                proposal = response.json()
                if proposal["job"]["status"] == "succeeded":
                    continue
                with app.state.db.transaction(ids["tenant_id"], write=True) as session:
                    row = session.get(AIProposal, previous.id)
                    job = session.get(Job, row.job_id)
                    if job.status not in {"failed", "cancelled"}:
                        raise SystemExit("The saved evaluation job is not ready to retry")
                    checkpoints = session.scalars(select(Job.checkpoint).where(Job.kind == "ai.propose")).all()
                    row.inputs = {
                        **row.inputs,
                        "budget_cny": retry_budget_cny(
                            row.inputs["budget_cny"], report["budget_cny"], checkpoints, job.checkpoint
                        ),
                    }
                response = client.post(f"/api/jobs/{previous.job_id}/retry")
                response.raise_for_status()
            else:
                response = client.post(
                    "/api/ai/proposals",
                    json={
                        "project_id": ids["project_id"],
                        "connection_id": connection["id"],
                        "document_ids": [document["id"]],
                        "pages": {document["id"]: selected},
                        "instructions": goal,
                        "quality": quality,
                        "budget_cny": max(0.001, remaining),
                    },
                )
                response.raise_for_status()
                proposal = response.json()
            started = time.monotonic()
            worker_error = []

            def work(job_id=proposal["job_id"], errors=worker_error):
                try:
                    while process_one(app.state.db, settings):
                        with app.state.db.transaction(ids["tenant_id"]) as session:
                            job = session.get(Job, job_id)
                            if job.status in {"failed", "cancelled", "succeeded"}:
                                break
                except Exception as exc:  # noqa: BLE001 - never print private provider/file error payloads
                    errors.append(type(exc).__name__)

            thread = threading.Thread(target=work)
            thread.start()
            while thread.is_alive():
                thread.join(20)
                with app.state.db.transaction(ids["tenant_id"]) as session:
                    job = session.get(Job, proposal["job_id"])
                    cp = job.checkpoint
                    print(
                        json.dumps(
                            {
                                "quality": quality,
                                "status": job.status,
                                "stage": cp.get("stage"),
                                "pages_completed": cp.get("pages_completed", 0),
                                "cost_cny": cp.get("cost_cny", 0),
                            },
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )
            with app.state.db.transaction(ids["tenant_id"]) as session:
                job = session.get(Job, proposal["job_id"])
                row = session.get(AIProposal, proposal["id"])
                cp = job.checkpoint
                result = {
                    "quality": quality,
                    "status": job.status,
                    "pages_completed": cp.get("pages_completed", 0),
                    "pages_selected": len(selected),
                    "candidate_count": len(row.candidates),
                    "relation_count": len(row.relations),
                    "gap_count": len(row.gaps),
                    "cost_cny": cp.get("cost_cny", 0),
                    "reserved_cny": cp.get("reserved_cny", 0),
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                    "worker_errors": worker_error,
                    "error": job.error,
                    "error_code": job.result.get("error_code"),
                    "proposal_id": row.id,
                }
                report["runs"].append(result)
                (output / "summary.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(json.dumps(result, ensure_ascii=True), flush=True)
            remaining = remaining_budget()
            report["remaining_budget_cny"] = remaining
            (output / "summary.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if remaining < 0.1 or (
                result["status"] != "succeeded"
                and result["error_code"] not in {"model_output_truncated", "model_schema_invalid"}
            ):
                break
    print(
        json.dumps(
            {
                "completed_runs": len(report["runs"]),
                "remaining_budget": remaining,
                "accuracy_measured": False,
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
