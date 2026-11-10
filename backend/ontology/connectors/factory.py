"""Construct configured read adapters; credentials are supplied by the secret store."""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr, ValidationError

from ontology.connectors.base import Connector, ConnectorError
from ontology.connectors.policy import NetworkPolicy


def create_connector(
    kind: str, config: dict[str, Any], *, network_policy: NetworkPolicy | None = None
) -> Connector:
    try:
        if kind in {"sqlite", "postgres", "postgresql", "mysql"}:
            from ontology.connectors.sql import SQLConfig, SQLConnector

            return SQLConnector(
                SQLConfig(**{**config, "driver": "postgres" if kind == "postgresql" else kind}),
                network_policy=network_policy,
            )
        if kind == "file":
            from ontology.connectors.files import FileConnector

            return FileConnector(
                config["root"], config["resources"], max_bytes=config.get("max_bytes", 50 * 1024 * 1024)
            )
        if kind == "rdf":
            from ontology.connectors.rdf import RDFConnector

            data = config["data"]
            return RDFConnector(
                data.encode("utf-8") if isinstance(data, str) else data,
                format=config.get("format", "turtle"),
                max_bytes=config.get("max_bytes", 20 * 1024 * 1024),
            )
        if kind not in {"mongo", "mongodb", "neo4j", "http", "feishu"}:
            raise ConnectorError("unsupported_connector", "Connector kind is not implemented")
        if network_policy is None:
            raise ConnectorError("network_denied", "Network adapters require an explicit allowed-host policy")
        if kind in {"mongo", "mongodb"}:
            from ontology.connectors.mongo import MongoConfig, MongoConnector

            return MongoConnector(MongoConfig(**config), network_policy=network_policy)
        if kind == "neo4j":
            from ontology.connectors.neo4j import Neo4jConfig, Neo4jConnector

            return Neo4jConnector(Neo4jConfig(**config), network_policy=network_policy)
        from ontology.connectors.http import HTTPConnector, HTTPResource, SafeHTTPClient

        token = config.get("token")
        client = SafeHTTPClient(
            config.get("base_url", "https://open.feishu.cn/open-apis/"),
            network_policy=network_policy,
            token=SecretStr(token) if isinstance(token, str) else token,
            max_bytes=config.get("max_bytes", 10 * 1024 * 1024),
            timeout=config.get("timeout_seconds", 20),
        )
        if kind == "http":
            return HTTPConnector(client, [HTTPResource(**r) for r in config["resources"]])
        from ontology.connectors.feishu import FeishuConnector, FeishuResource

        return FeishuConnector(
            client,
            [FeishuResource(**r) for r in config["resources"]],
            credential_kind=config.get("credential_kind", "user"),
        )
    except ConnectorError:
        raise
    except (ValidationError, KeyError, ValueError, TypeError, OSError):
        raise ConnectorError("invalid_config", "Connector configuration is incomplete or invalid") from None
