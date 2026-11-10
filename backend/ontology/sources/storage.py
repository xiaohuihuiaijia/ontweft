"""Offline inventory of uploaded table bundles; retained evidence is never removed."""

import json
import re
import time
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from ontology.identity.models import Tenant
from ontology.sources.models import Source, SourceFileVersion
from ontology.sources.tabular_connector import bundle_path


def inventory(db, settings, *, min_age_days=7, quarantine=False):
    if min_age_days < 1:
        raise ValueError("隔离前至少保留 1 天；默认保留 7 天")
    root = settings.data_dir.resolve()
    referenced = set()
    with db.transaction() as session:
        tenants = list(session.scalars(select(Tenant.id)))
    for tenant in tenants:
        with db.transaction(tenant) as session:
            for source in session.scalars(
                select(Source).where(Source.tenant_id == tenant, Source.kind == "tabular")
            ):
                referenced.add(bundle_path(settings, source).resolve())
            for version in session.scalars(
                select(SourceFileVersion).where(SourceFileVersion.tenant_id == tenant)
            ):
                holder = SimpleNamespace(
                    tenant_id=tenant,
                    project_id=version.project_id,
                    id=version.source_id,
                    config=version.config,
                )
                referenced.add(bundle_path(settings, holder).resolve())
    candidates = []
    for name, pattern in (("uploaded_tables", "*/*/*"), ("uploaded_versions", "*")):
        parent = root / name
        if parent.is_symlink() or not parent.resolve().is_relative_to(root):
            raise ValueError("上传目录不得指向数据目录之外")
        for path in parent.glob(pattern):
            if not path.is_dir():
                continue
            # Only the two uploader-owned layouts are eligible. Unknown files stay untouched.
            parts = path.relative_to(parent).parts
            known = (len(parts) == 1 and re.fullmatch(r"[0-9a-f]{64}", parts[0])) or (
                len(parts) == 3
                and all(re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", part) for part in parts)
            )
            if not known:
                continue
            descendants = list(path.rglob("*"))
            if any(p.is_symlink() for p in [path, *path.parents[: len(parts) - 1], *descendants]):
                continue
            files = [p for p in descendants if p.is_file()]
            if not path.resolve().is_relative_to(root):
                continue
            size = sum(p.stat().st_size for p in files)
            modified = max([path.stat().st_mtime, *(p.stat().st_mtime for p in descendants)])
            status = (
                "referenced"
                if path.resolve() in referenced
                else ("recent" if time.time() - modified < min_age_days * 86400 else "orphan")
            )
            candidates.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": size,
                    "files": len(files),
                    "status": status,
                }
            )
    result = {
        "policy": "retain_all_versions",
        "min_age_days": min_age_days,
        "items": sorted(candidates, key=lambda item: item["path"]),
        "totals": {
            state: {
                "bundles": sum(i["status"] == state for i in candidates),
                "bytes": sum(i["bytes"] for i in candidates if i["status"] == state),
            }
            for state in ("referenced", "recent", "orphan")
        },
    }
    if quarantine and any(item["status"] == "orphan" for item in candidates):
        destination = root / "quarantine" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
        if not destination.resolve().is_relative_to(root) or (root / "quarantine").is_symlink():
            raise ValueError("隔离目录不得指向数据目录之外")
        destination.mkdir(parents=True)
        manifest = destination / "manifest.json"
        result["quarantine_manifest"] = manifest.relative_to(root).as_posix()
        # Persist the planned moves first; a failed/partial run remains recoverable.
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        for item in candidates:
            if item["status"] != "orphan":
                continue
            source, target = root / item["path"], destination / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
    return result
