"""Read-only connector for an immutable, tenant-scoped uploaded table bundle."""

import hashlib
import json

from ontology.connectors.base import (
    ConnectionCheck,
    ConnectorBase,
    ConnectorCapabilities,
    ConnectorError,
    FieldSchema,
    ResourceSchema,
    SnapshotPage,
    SourceRecord,
    canonical_identity_key,
    validate_limit,
)
from ontology.sources.tabular import MAX_EXPANDED_BYTES, Table, describe_table


def bundle_path(settings, source):
    root = (settings.data_dir / "uploaded_tables").resolve()
    path = (root / source.tenant_id / source.project_id / source.id).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ConnectorError("upload_path_denied", "上传来源路径不合法")
    if source.config.get("bundle_id"):
        # A scoped digest avoids Windows MAX_PATH growth with nested UUIDs.
        identity = "/".join((source.tenant_id, source.project_id, source.id, source.config["bundle_id"]))
        path = (
            settings.data_dir / "uploaded_versions" / hashlib.sha256(identity.encode()).hexdigest()
        ).resolve()
    return path


def serialize_tables(tables):
    return json.dumps(
        [
            {
                "name": t.name,
                "columns": t.columns,
                "rows": t.rows,
                "source_rows": t.source_rows,
                "blank_rows": t.blank_rows,
                "formula_cells": t.formula_cells,
            }
            for t in tables
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


class UploadedTablesConnector(ConnectorBase):
    capabilities = ConnectorCapabilities(
        consistent_snapshot=True,
        resumable_after_restart=True,
        notes=["不可变文件快照；已配置业务键时跨版本更新，行号仅用于定位"],
    )

    def __init__(self, source, settings):
        self.config = source.config
        path = bundle_path(settings, source) / "tables.json"
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_EXPANDED_BYTES + 1)
            if (
                len(data) > MAX_EXPANDED_BYTES
                or hashlib.sha256(data).hexdigest() != self.config["tables_sha256"]
            ):
                raise ValueError("digest mismatch")
            self.tables = [Table(**item) for item in json.loads(data)]
        except (OSError, ValueError, TypeError, KeyError):
            raise ConnectorError("upload_unavailable", "上传快照不存在或完整性校验未通过") from None

    def test_connection(self):
        return ConnectionCheck(ok=True, message="上传快照可读取，内容校验通过")

    def discover(self):
        result = []
        for index, table in enumerate(self.tables):
            profile = describe_table(table)
            fields = [
                FieldSchema(
                    name="_source_row",
                    data_type="integer",
                    nullable=False,
                    metadata={"technical": True, "label": "原始行号"},
                )
            ]
            fields.extend(
                FieldSchema(
                    name=f"c{number + 1}",
                    data_type="string",
                    ordinal=number + 1,
                    metadata={**field, "label": table.columns[number]},
                )
                for number, field in enumerate(profile["fields"])
            )
            result.append(
                ResourceSchema(
                    resource_id=f"sheet_{index + 1}",
                    name=table.name,
                    fields=self.config.get("frozen_fields", {}).get(f"sheet_{index + 1}", fields),
                    primary_key=self.config.get("business_keys", {}).get(f"sheet_{index + 1}")
                    or ["_source_row"],
                    metadata={
                        **profile,
                        "content_sha256": self.config["content_sha256"],
                        "identity_scope": "immutable_upload_row",
                        "source_filename": self.config["filename"],
                    },
                )
            )
        return result

    def sample(self, resource, limit=20):
        return self.read_snapshot(resource, limit=limit)

    def read_snapshot(self, resource, *, cursor=None, limit=1000):
        validate_limit(limit)
        index = next((i for i in range(len(self.tables)) if resource == f"sheet_{i + 1}"), None)
        if index is None:
            raise ConnectorError("unknown_resource", "上传工作表不存在")
        token = self.config["tables_sha256"] + ":" + resource + ":"
        try:
            if cursor is not None and not cursor.startswith(token):
                raise ValueError()
            offset = int(cursor[len(token) :]) if cursor else 0
            table = self.tables[index]
            if not 0 <= offset <= len(table.rows):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ConnectorError("invalid_cursor", "上传快照游标不属于当前资源") from None
        end = min(offset + limit, len(table.rows))
        records = []
        for row in range(offset, end):
            values = {
                "_source_row": table.source_rows[row],
                **{f"c{i + 1}": value for i, value in enumerate(table.rows[row])},
            }
            fields = self.config.get("business_keys", {}).get(resource) or ["_source_row"]
            key = {field: values[field] for field in fields}
            records.append(
                SourceRecord(
                    key=key,
                    identity_key=canonical_identity_key(key),
                    values=values,
                )
            )
        return SnapshotPage(
            records=records,
            resource_id=resource,
            snapshot_id=self.config["tables_sha256"],
            consistency="transaction",
            next_cursor=token + str(end) if end < len(table.rows) else None,
            complete=end == len(table.rows),
            warnings=self.capabilities.notes,
        )

    def close(self):
        pass
