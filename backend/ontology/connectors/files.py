"""Explicitly selected local files. Logical IDs are supplied, not guessed from names."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ontology.connectors.base import (
    ConnectionCheck,
    ConnectorBase,
    ConnectorCapabilities,
    ConnectorError,
    ResourceSchema,
    SnapshotPage,
    SourceRecord,
    validate_limit,
)


class FileConnector(ConnectorBase):
    capabilities = ConnectorCapabilities(
        consistent_snapshot=True,
        notes=["Each file is independently read and hashed; no atomic directory snapshot"],
    )

    def __init__(self, root: str | Path, resources: dict[str, str], *, max_bytes: int = 50 * 1024 * 1024):
        self.root = Path(root).resolve(strict=True)
        self.resources = dict(resources)
        self.max_bytes = max_bytes

    def _path(self, resource: str) -> Path:
        if resource not in self.resources:
            raise ConnectorError("unknown_resource", "File is outside the selected inventory")
        try:
            path = (self.root / self.resources[resource]).resolve(strict=True)
        except OSError:
            raise ConnectorError("source_missing", "Selected file is unavailable") from None
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ConnectorError("path_denied", "Selected file is outside its permitted root")
        return path

    def test_connection(self) -> ConnectionCheck:
        try:
            self.discover()
            return ConnectionCheck(ok=True)
        except ConnectorError as exc:
            return ConnectionCheck(ok=False, code=exc.code, message=str(exc))

    def discover(self) -> list[ResourceSchema]:
        result = []
        for key in self.resources:
            path = self._path(key)
            result.append(
                ResourceSchema(
                    resource_id=key,
                    name=path.name,
                    kind="file",
                    primary_key=["resource_id"],
                    metadata={"bytes": path.stat().st_size},
                )
            )
        return result

    def read_snapshot(self, resource: str, *, cursor: str | None = None, limit: int = 1000) -> SnapshotPage:
        validate_limit(limit)
        if cursor:
            raise ConnectorError("invalid_cursor", "A file snapshot has only one page")
        path = self._path(resource)
        with path.open("rb") as stream:
            before = path.stat()
            data = stream.read(self.max_bytes + 1)
            after = path.stat()
        if len(data) > self.max_bytes:
            raise ConnectorError("resource_limit", "File exceeds its byte budget")
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ConnectorError("source_changed", "File changed during collection", retryable=True)
        digest = hashlib.sha256(data).hexdigest()
        return SnapshotPage(
            resource_id=resource,
            snapshot_id=digest,
            complete=True,
            consistency="collection",
            records=[
                SourceRecord(
                    key={"resource_id": resource},
                    values={"filename": path.name, "content_hash": digest, "content": data},
                )
            ],
        )

    def sample(self, resource: str, limit: int = 20) -> SnapshotPage:
        return self.read_snapshot(resource, limit=limit)

    def close(self) -> None:
        pass
