"""Offline RDF input; no endpoint, remote context or external entity resolution."""

from __future__ import annotations

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
    validate_limit,
)


class RDFConnector(ConnectorBase):
    capabilities = ConnectorCapabilities(
        consistent_snapshot=True,
        notes=[
            "Immutable byte input only; blank-node identity is scoped to content revision and named graph"
        ],
    )

    def __init__(
        self,
        data: bytes,
        *,
        format: str = "turtle",
        max_bytes: int = 20 * 1024 * 1024,
        max_triples: int = 500_000,
    ):
        if len(data) > max_bytes:
            raise ConnectorError("resource_limit", "RDF input exceeds its byte budget")
        self.data, self.format, self.max_triples = data, format, max_triples
        self.revision = hashlib.sha256(data).hexdigest()
        self._records: dict[str, list[SourceRecord]] | None = None

    def _load(self) -> dict[str, list[SourceRecord]]:
        if self._records is not None:
            return self._records
        if self.format not in {"turtle", "xml", "json-ld", "trig", "nt", "nquads"}:
            raise ConnectorError("unsupported_format", "RDF format is not enabled")
        try:
            if self.format == "xml":
                from defusedxml.ElementTree import fromstring

                fromstring(self.data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
            if self.format == "json-ld":
                value = json.loads(self.data)

                def check(node, context=False):
                    if isinstance(node, str) and context:
                        raise ConnectorError("network_denied", "Remote JSON-LD contexts are disabled")
                    if isinstance(node, list):
                        for item in node:
                            check(item, context)
                    elif isinstance(node, dict):
                        if "@import" in node:
                            raise ConnectorError("network_denied", "JSON-LD context imports are disabled")
                        for key, item in node.items():
                            check(item, key == "@context")

                check(value)
            from rdflib import BNode, Dataset, Literal
            from rdflib.compare import to_canonical_graph

            dataset = Dataset()
            dataset.parse(data=self.data, format=self.format, publicID="urn:ontology:input:" + self.revision)
            if len(dataset) > self.max_triples:
                raise ConnectorError("resource_limit", "RDF graph exceeds its triple budget")
            result = {}
            for graph in dataset.graphs():
                if not len(graph):
                    continue
                graph_id = str(graph.identifier)
                # Canonicalization gives deterministic local blank-node labels for re-import.
                canonical = to_canonical_graph(graph)

                def term(value, graph_id=graph_id):
                    if isinstance(value, BNode):
                        return {"kind": "blank", "value": f"{self.revision}:{graph_id}:{value}"}
                    if isinstance(value, Literal):
                        return {
                            "kind": "literal",
                            "value": str(value),
                            "language": value.language,
                            "datatype": str(value.datatype) if value.datatype else None,
                        }
                    return {"kind": "uri", "value": str(value)}

                rows = []
                for subject, predicate, obj in canonical:
                    values = {
                        "subject": term(subject),
                        "predicate": term(predicate),
                        "object": term(obj),
                        "graph": graph_id,
                    }
                    key = hashlib.sha256(
                        json.dumps(values, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest()
                    rows.append(SourceRecord(key={"triple_id": key}, values=values))
                result[graph_id] = sorted(rows, key=lambda r: r.key["triple_id"])
            self._records = result
            return result
        except ConnectorError:
            raise
        except ImportError:
            raise ConnectorError("driver_unavailable", "RDF parser dependencies are not installed") from None
        except Exception:  # noqa: BLE001 -- public adapter boundary redacts untrusted driver errors.
            raise ConnectorError("invalid_rdf", "RDF cannot be decoded safely") from None

    def discover(self) -> list[ResourceSchema]:
        return [
            ResourceSchema(
                resource_id=key,
                name=key,
                kind="rdf_graph",
                primary_key=["triple_id"],
                fields=[
                    FieldSchema(name=n, data_type="rdf_term") for n in ("subject", "predicate", "object")
                ],
            )
            for key in self._load()
        ]

    def test_connection(self) -> ConnectionCheck:
        try:
            self._load()
            return ConnectionCheck(ok=True)
        except ConnectorError as exc:
            return ConnectionCheck(ok=False, code=exc.code, message=str(exc))

    def read_snapshot(self, resource: str, *, cursor: str | None = None, limit: int = 1000) -> SnapshotPage:
        validate_limit(limit)
        rows = self._load().get(resource)
        if rows is None:
            raise ConnectorError("unknown_resource", "Unknown RDF graph")
        prefix = hashlib.sha256((self.revision + resource).encode()).hexdigest()
        try:
            if cursor and not cursor.startswith(prefix + ":"):
                raise ValueError()
            offset = int(cursor.split(":")[-1]) if cursor else 0
            if offset < 0 or offset > len(rows):
                raise ValueError()
        except ValueError:
            raise ConnectorError("invalid_cursor", "RDF cursor does not match this graph revision") from None
        end = min(offset + limit, len(rows))
        return SnapshotPage(
            resource_id=resource,
            snapshot_id=self.revision,
            complete=end == len(rows),
            next_cursor=f"{prefix}:{end}" if end < len(rows) else None,
            records=rows[offset:end],
            consistency="collection",
        )

    def sample(self, resource: str, limit: int = 20) -> SnapshotPage:
        return self.read_snapshot(resource, limit=limit)

    def close(self) -> None:
        self._records = None
