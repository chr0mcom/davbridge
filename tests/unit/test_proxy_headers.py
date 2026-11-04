"""`server.trusted_proxies`: forwarded headers are honoured only from the configured peers (default: nobody)."""

import dataclasses

import httpx
import pytest
from tests import soap_helpers as h
from tests.unit.conftest import KEY, USER, config_for

from davbridge.main import build_services, create_app
from davbridge.operations import import_all
from davbridge.proxy_headers import parse_networks, resolve_client

NETWORKS = parse_networks(["10.0.0.0/8", "172.18.0.2/32", "fd00::/8"])


@pytest.mark.parametrize(
    "peer,xff,expected",
    [
        ("10.0.0.5", "203.0.113.9", "203.0.113.9"),
        ("10.0.0.5", "198.51.100.7, 203.0.113.9", "203.0.113.9"),
        ("10.0.0.5", "198.51.100.7, 10.0.0.9", "198.51.100.7"),
        ("10.0.0.5", "10.0.0.7, 10.0.0.9", "10.0.0.7"),
        ("10.0.0.5", "1.1.1.1, 203.0.113.9, 10.1.1.1", "203.0.113.9"),
        ("172.18.0.2", "2001:db8::1", "2001:db8::1"),
        ("fd00::1", "203.0.113.9", "203.0.113.9"),
        ("198.51.100.1", "203.0.113.9", "198.51.100.1"),
        ("172.18.0.3", "203.0.113.9", "172.18.0.3"),
        ("10.0.0.5", None, "10.0.0.5"),
        ("10.0.0.5", "", "10.0.0.5"),
        ("10.0.0.5", "not-an-ip", "10.0.0.5"),
        ("10.0.0.5", "203.0.113.9, junk", "10.0.0.5"),
        ("10.0.0.5", ", ".join(["1.1.1.1"] * 30), "10.0.0.5"),
        ("unknown", "203.0.113.9", "unknown"),
    ],
)
def test_resolve_client(peer, xff, expected):
    assert resolve_client(peer, xff, NETWORKS) == expected


def test_without_trusted_proxies_nothing_is_evaluated():
    assert resolve_client("10.0.0.5", "203.0.113.9", ()) == "10.0.0.5"


async def wrong_login(fake, clock, tmp_path, proxies, peer, xff):
    import_all()
    built = {}

    async def factory():
        k = dataclasses.replace(config_for(tmp_path, failure_limit=3), trusted_proxies=proxies)
        d = await build_services(k, KEY, clock, caldav=fake)
        d.notifier.watch = lambda account: None
        built["d"] = d
        return d

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=(peer, 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            header = {**h.basic(USER, "wrong-password-1"), **({"X-Forwarded-For": xff} if xff else {})}
            for _ in range(3):
                await client.post("/EWS/Exchange.asmx", content=h.envelope(h.get_folder("calendar")), headers=header)
        return await built["d"].store.locks_list(), built["d"]


async def test_default_ignores_x_forwarded_for(fake, clock, tmp_path):
    listing, _ = await wrong_login(fake, clock, tmp_path, (), "10.0.0.5", "203.0.113.9")
    assert listing == ["10.0.0.5"]


async def test_trusted_proxy_yields_the_client_address(fake, clock, tmp_path):
    listing, _ = await wrong_login(fake, clock, tmp_path, ("10.0.0.0/8",), "10.0.0.5", "203.0.113.9")
    assert listing == ["203.0.113.9"]


async def test_foreign_peer_cannot_forge_the_address(fake, clock, tmp_path):
    listing, _ = await wrong_login(fake, clock, tmp_path, ("10.0.0.0/8",), "198.51.100.1", "203.0.113.9")
    assert listing == ["198.51.100.1"]
