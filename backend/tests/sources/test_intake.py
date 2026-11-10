import hashlib
import io
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from ontology.connectors import ConnectorError
from ontology.identity.models import Membership
from ontology.sources.models import Source
from ontology.sources.service import connector_for
from ontology.sources.tabular import TabularError, read_tables
from ontology.sources.tabular_connector import bundle_path
from openpyxl import Workbook
from sqlalchemy import select

from tests.support.ingestion import capture, make_source
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


def upload(client, project_id, content=None):
    content = (
        content
        or "编号,名称,数量,金额\n001,通用对象甲,9223372036854775809,1.20\n002,通用对象乙,,0\n".encode(
            "utf-8-sig"
        )
    )
    response = client.post(
        "/api/sources/upload",
        data={"project_id": project_id, "name": "我的数据"},
        files={"file": ("数据.csv", content, "text/csv")},
    )
    assert response.status_code == 201, response.text
    return response.json(), content


def proposal(client, source, **changes):
    return client.post(
        f"/api/sources/{source['id']}/model-proposal",
        json={
            "expected_source_revision": source["revision"],
            "resource_ids": [source["resources"][0]["resource_id"]],
            **changes,
        },
    )


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_upload_edit_publish_and_project_preserves_evidence_and_stable_identity(request, fixture_name):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    source, content = upload(client, owner["project_id"])
    resource = source["resources"][0]
    assert source["kind"] == "tabular" and resource["metadata"]["row_count"] == 2
    assert source["config"]["content_sha256"] == hashlib.sha256(content).hexdigest()
    assert client.get(f"/api/sources/{source['id']}/original").content == content
    sample = client.get(
        f"/api/sources/{source['id']}/sample", params={"resource": resource["resource_id"], "limit": 1}
    )
    assert sample.status_code == 200, sample.text
    assert sample.json()["records"][0]["values"]["c1"] == "001"
    draft = proposal(client, source).json()
    assert draft["baseline_release_id"] is None
    assert client.get("/api/changesets", params={"project_id": owner["project_id"]}).json()["total"] == 0
    assert client.get("/api/objects", params={"project_id": owner["project_id"]}).json()["total"] == 0
    model = draft["definition"]
    model["types"][0]["label"] = "自定义业务对象"
    model["types"][0]["properties"].append({"key": "note", "label": "人工备注", "data_type": "string"})
    publish(client, owner["project_id"], model)
    first = capture(client, app, source, resource)
    assert first["snapshot"]["status"] == "complete", first
    assert first["snapshot"]["projection"]["created"] == 2
    objects = client.get("/api/objects", params={"project_id": owner["project_id"]}).json()["items"]
    item = next(row for row in objects if row["properties"]["name"] == "通用对象甲")
    # Existing API lossless-wire convention stringifies integers beyond JS safe range.
    assert item["properties"]["c1"] == "001" and item["properties"]["c3"] == "9223372036854775809"
    assert item["properties"]["c4"] == "1.20"
    edited = client.patch(
        f"/api/objects/{item['id']}",
        json={"expected_revision": item["revision"], "properties": {"note": "人工补充"}},
    )
    assert edited.status_code == 200, edited.text
    second = capture(client, app, source, resource)
    assert second["snapshot"]["projection"]["created"] == 0
    assert client.get(f"/api/objects/{item['id']}").json()["properties"]["note"] == "人工补充"
    assert first["items"][0]["source_key"]["_source_row"] in {2, 3}
    assert proposal(client, source).status_code == 409


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_database_and_upload_share_proposals_and_preserve_existing_model(request, fixture_name):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    existing, _, _, _ = make_source(client, app, owner["project_id"])
    baseline = client.get("/api/ontology/definition", params={"project_id": owner["project_id"]}).json()
    uploaded, _ = upload(client, owner["project_id"])
    result = proposal(client, uploaded)
    assert result.status_code == 200, result.text
    assert result.json()["baseline_release_id"] == baseline["release_id"]
    assert result.json()["definition"]["types"][0] == baseline["definition"]["types"][0]
    assert result.json()["definition"]["mappings"][0] == baseline["definition"]["mappings"][0]
    # A distinct connection to the same explicit SQL schema gets a separate editable model.
    copy = client.post(
        "/api/sources",
        json={
            "project_id": owner["project_id"],
            "name": "第二来源",
            "kind": "sqlite",
            "config": existing["config"],
        },
    ).json()
    copy = client.post(f"/api/sources/{copy['id']}/discover").json()["source"]
    generated = proposal(client, copy)
    assert generated.status_code == 200, generated.text
    assert generated.json()["definition"]["types"][-1]["properties"][0]["data_type"] == "integer"
    assert proposal(client, uploaded, expected_source_revision=999).status_code == 409
    assert proposal(client, uploaded, resource_ids=["not-real"]).status_code == 422


def test_upload_authority_and_failed_input_leave_no_sources_or_files(platform):
    client, app, owner, other = platform
    root = app.state.settings.data_dir / "uploaded_tables"
    denied = client.post(
        "/api/sources/upload",
        data={"project_id": other["project_id"]},
        files={"file": ("x.csv", b"name\none")},
    )
    assert denied.status_code in {403, 404}
    bad = client.post(
        "/api/sources/upload",
        data={"project_id": owner["project_id"]},
        files={"file": ("x.csv", b"name,name\na,b")},
    )
    assert bad.status_code == 422 and not root.exists()
    source, _ = upload(client, owner["project_id"])
    with app.state.db.transaction(owner["tenant_id"]) as session:
        member = session.scalar(select(Membership).where(Membership.project_id == owner["project_id"]))
        member.role = "viewer"
    assert proposal(client, source).status_code == 403
    assert client.get(f"/api/sources/{source['id']}/original").status_code == 403
    client.post("/api/auth/logout")
    client.post(
        "/api/auth/login",
        json={"tenant_slug": "beta", "email": "owner@example.com", "password": "Safe-Test-Password-2026"},
    )
    assert proposal(client, source).status_code == 404
    assert client.get(f"/api/sources/{source['id']}/original").status_code == 404


def test_uploaded_bundle_is_isolated_integrity_checked_and_cursor_bound(platform):
    client, app, owner, _ = platform
    source, _ = upload(client, owner["project_id"])
    with app.state.db.transaction(owner["tenant_id"]) as session:
        record = session.get(Source, source["id"])
        connector = connector_for(record, app.state.settings)
        page = connector.read_snapshot("sheet_1", limit=1)
        assert not page.complete
        assert connector.read_snapshot("sheet_1", cursor=page.next_cursor).records[0].values["c1"] == "002"
        with pytest.raises(ConnectorError, match="游标"):
            connector.read_snapshot("sheet_1", cursor="different:1")
        path = bundle_path(app.state.settings, record)
        assert not path.is_relative_to(app.state.settings.data_dir / "imports")
        (path / "tables.json").write_text("[]")
        (path / "original").write_bytes(b"altered")
    assert client.post(f"/api/sources/{source['id']}/test").status_code == 422
    assert client.get(f"/api/sources/{source['id']}/original").status_code == 409


def test_multisheet_upload_creates_distinct_editable_types_and_imports_each_table(platform):
    client, app, owner, _ = platform
    book = Workbook()
    for index, title in enumerate(["第一类", "第二类"]):
        sheet = book.active if index == 0 else book.create_sheet()
        sheet.title = title
        sheet.append(["编号", "名称"])
        sheet.append(["001", title + "对象"])
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()
    response = client.post(
        "/api/sources/upload",
        data={"project_id": owner["project_id"]},
        files={"file": ("多表.xlsx", buffer.getvalue())},
    )
    assert response.status_code == 201, response.text
    source = response.json()
    result = proposal(client, source, resource_ids=[r["resource_id"] for r in source["resources"]])
    assert result.status_code == 200, result.text
    model = result.json()["definition"]
    assert len({item["key"] for item in model["types"]}) == 2
    publish(client, owner["project_id"], model)
    for resource in source["resources"]:
        captured = capture(client, app, source, resource)
        assert captured["snapshot"]["projection"]["created"] == 1
    objects = client.get("/api/objects", params={"project_id": owner["project_id"]}).json()
    assert objects["total"] == 2
    assert {item["properties"]["name"] for item in objects["items"]} == {"第一类对象", "第二类对象"}


def test_upload_rechecks_permission_and_removes_only_its_uncommitted_files(platform, monkeypatch):
    from ontology.sources import intake_api

    client, app, owner, _ = platform
    original = intake_api.serialize_tables

    def revoke_after_read(tables):
        with app.state.db.transaction(owner["tenant_id"]) as session:
            membership = session.scalar(
                select(Membership).where(Membership.project_id == owner["project_id"])
            )
            membership.role = "viewer"
        return original(tables)

    monkeypatch.setattr(intake_api, "serialize_tables", revoke_after_read)
    response = client.post(
        "/api/sources/upload",
        data={"project_id": owner["project_id"]},
        files={"file": ("x.csv", b"name\nsynthetic")},
    )
    assert response.status_code == 403
    root = app.state.settings.data_dir / "uploaded_tables"
    assert not any(path.is_file() for path in root.rglob("*"))
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert not session.scalars(select(Source)).all()


def test_xlsx_declared_dimensions_cannot_hide_rows_or_bypass_budget():
    book = Workbook()
    sheet = book.active
    sheet.append(["名称"])
    sheet.append(["first"])
    sheet.append(["second"])
    data = io.BytesIO()
    book.save(data)
    book.close()

    def changed(old, new):
        output = io.BytesIO()
        with ZipFile(io.BytesIO(data.getvalue())) as original, ZipFile(output, "w", ZIP_DEFLATED) as archive:
            for entry in original.infolist():
                value = original.read(entry.filename)
                if entry.filename == "xl/worksheets/sheet1.xml":
                    value = value.replace(old, new)
                archive.writestr(entry.filename, value)
        return output.getvalue()

    (table,) = read_tables(changed(b'ref="A1:A3"', b'ref="A1:A1"'), "book.xlsx")
    assert len(table.rows) == 2
    with pytest.raises(TabularError, match="预算"):
        read_tables(changed(b'r="A3"', b'r="XFD999999"'), "book.xlsx")
    with pytest.raises(TabularError, match="XLSX"):
        read_tables(
            changed(b"<worksheet ", b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///forbidden">]><worksheet '),
            "book.xlsx",
        )
