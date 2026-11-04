"""Forwarded headers (`X-Forwarded-For`, `X-Forwarded-Proto`) are honoured only from the configured trusted proxies.

Pure ASGI middleware. The trusted networks come from `server.trusted_proxies` of the running configuration; with the default
(empty list) the headers are ignored and the peer address of the TCP connection is the client address.
"""

from __future__ import annotations

import ipaddress
from typing import Iterable

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(values: Iterable[str]) -> tuple[IPNetwork, ...]:
    return tuple(ipaddress.ip_network(v, strict=False) for v in values)


def _is_trusted(address: ipaddress.IPv4Address | ipaddress.IPv6Address, networks: tuple[IPNetwork, ...]) -> bool:
    return any(address in n for n in networks if n.version == address.version)


def resolve_client(peer: str, forwarded_for: str | None, networks: tuple[IPNetwork, ...]) -> str:
    """Client address behind trusted proxies: walk `X-Forwarded-For` from the right and stop at the first address that is not a trusted proxy."""
    if not networks or not forwarded_for:
        return peer
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not _is_trusted(peer_address, networks):
        return peer
    chain = [part.strip() for part in forwarded_for.split(",")]
    if len(chain) > 20:
        return peer
    try:
        addresses = [ipaddress.ip_address(part) for part in chain]
    except ValueError:
        return peer
    for address in reversed(addresses):
        if not _is_trusted(address, networks):
            return str(address)
    return str(addresses[0])


class ProxyHeaders:
    def __init__(self, app) -> None:
        self.app = app
        self._cache: tuple[tuple[str, ...], tuple[IPNetwork, ...]] = ((), ())

    def _networks(self, scope) -> tuple[IPNetwork, ...]:
        state = getattr(scope.get("app"), "state", None)
        services = getattr(state, "d", None)
        config = getattr(services, "config", None)
        configured = tuple(getattr(config, "trusted_proxies", ()) or ())
        if configured != self._cache[0]:
            self._cache = (configured, parse_networks(configured))
        return self._cache[1]

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket") and scope.get("client"):
            networks = self._networks(scope)
            if networks:
                headers = {k: v for k, v in scope.get("headers", ()) if k in (b"x-forwarded-for", b"x-forwarded-proto")}
                forwarded_for = headers.get(b"x-forwarded-for", b"").decode("latin-1") or None
                peer = scope["client"][0]
                client = resolve_client(peer, forwarded_for, networks)
                scope = dict(scope)
                if client != peer:
                    scope["client"] = (client, 0)
                proto = headers.get(b"x-forwarded-proto", b"").decode("latin-1").strip().lower()
                try:
                    trusted_peer = _is_trusted(ipaddress.ip_address(peer), networks)
                except ValueError:
                    trusted_peer = False
                if trusted_peer and proto in ("http", "https"):
                    scope["scheme"] = proto
        await self.app(scope, receive, send)
