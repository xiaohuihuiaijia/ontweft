import json
import os
import time

from ontology.sources.storage import inventory
from ontology.sources.models import SourceFileVersion
from sqlalchemy import select


def test_inventory_preserves_references_and_only_quarantines_old_known_orphans(platform):
    client, app, owner, _ = platform
    source = client.post(
        "/api/sources/upload",
        data={"project_id": owner["project_id"]},
        files={"file": ("data.csv", b"id,name\n1,One\n")},
    ).json()
    data = app.state.settings.data_dir
    old = data / "uploaded_versions" / ("f" * 64)
    old.mkdir(parents=True)
    (old / "original").write_bytes(b"orphan")
    stale = time.time() - 10 * 86400
    for path in (old / "original", old):
        os.utime(path, (stale, stale))
    recent = data / "uploaded_versions" / ("e" * 64)
    recent.mkdir()
    (recent / "original").write_bytes(b"pending")
    unknown = data / "uploaded_versions" / "operator-backup"
    unknown.mkdir()
    malformed = data / "uploaded_tables" / ("a" * 36) / ("b" * 36) / ("c" * 36)
    malformed.mkdir(parents=True)
    os.utime(malformed, (stale, stale))
    result = inventory(app.state.db, app.state.settings)
    assert result["totals"]["referenced"]["bundles"] == 1
    assert result["totals"]["orphan"]["bytes"] == 6 and old.is_dir()
    moved = inventory(app.state.db, app.state.settings, quarantine=True)
    manifest = data / moved["quarantine_manifest"]
    assert json.loads(manifest.read_text(encoding="utf-8"))["policy"] == "retain_all_versions"
    assert (manifest.parent / "uploaded_versions" / old.name / "original").read_bytes() == b"orphan"
    assert not old.exists() and recent.exists() and unknown.exists() and malformed.exists()
    assert client.get(f"/api/sources/{source['id']}/original").status_code == 200


def test_versions_reach_older_than_100_with_stable_cursor_and_scoped_total(platform):
    client, app, owner, _ = platform
    source = client.post(
        "/api/sources/upload",
        data={"project_id": owner["project_id"]},
        files={"file": ("data.csv", b"id,name\n1,One\n")},
    ).json()
    with app.state.db.transaction(owner["tenant_id"]) as session:
        original = session.scalar(
            select(SourceFileVersion).where(SourceFileVersion.source_id == source["id"])
        )
        for number in range(2, 107):
            session.add(
                SourceFileVersion(
                    tenant_id=owner["tenant_id"],
                    project_id=owner["project_id"],
                    source_id=source["id"],
                    number=number,
                    base_revision=1,
                    config=original.config,
                    resources=original.resources,
                )
            )
    url = f"/api/sources/{source['id']}/versions"
    cursor, numbers = None, []
    while True:
        response = client.get(url, params={"limit": 20, **({"before": cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["total"] == 106 and page["original_bytes"] > 0
        numbers.extend(item["number"] for item in page["items"])
        cursor = page["next_before"]
        if cursor is None:
            break
    assert numbers == list(range(106, 0, -1))
