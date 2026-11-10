"""Explicit outbound policy. No wildcard/default-allow or redirect trust."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from ontology.connectors.base import ConnectorError


@dataclass(frozen=True)
class NetworkPolicy:
    allowed_hosts: frozenset[str]
    allow_private_addresses: bool = False
    allow_http: bool = False

    def check_host(self, host: str, port: int | None = None) -> None:
        name = host.rstrip(".").lower()
        if name not in {h.rstrip(".").lower() for h in self.allowed_hosts}:
            raise ConnectorError("network_denied", "Source host is not explicitly allowed")
        try:
            addresses = socket.getaddrinfo(name, port or 443, type=socket.SOCK_STREAM)
        except OSError:
            raise ConnectorError("dns_failed", "Source hostname cannot be resolved", retryable=True) from None
        if not self.allow_private_addresses and any(
            not ipaddress.ip_address(item[4][0]).is_global for item in addresses
        ):
            raise ConnectorError("network_denied", "Private source addresses require explicit configuration")

    def check_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ({"https", "http"} if self.allow_http else {"https"}):
            raise ConnectorError("network_denied", "Only explicitly enabled HTTP schemes are permitted")
        if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
            raise ConnectorError("invalid_url", "Credentials and fragments are not allowed in source URLs")
        try:
            port = parsed.port
        except ValueError:
            raise ConnectorError("invalid_url", "Invalid source port") from None
        self.check_host(parsed.hostname, port)
