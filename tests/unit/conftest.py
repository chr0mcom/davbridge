"""Fixtures for the application-level unit tests: FakeNextcloud instead of a real server, FakeClock instead of waiting."""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest
from tests import soap_helpers as h
from tests.conftest import TEST_CONFIG, as_test_accounts, mix
from tests.doubles.fake_nextcloud import FakeNextcloud

from davbridge import soap
from davbridge.auth import Session
from davbridge.caldav import Credentials
from davbridge.config import from_dict
from davbridge.main import build_services, create_app
from davbridge.operations import OPERATIONS, Context, import_all

PASSWORD = "pw-test-1234"
USER = "test-user"
TEST_CAL = "test-calendar"
READ_ONLY = "read-only-calendar_shared_by_read-only-owner"
KEY = bytes(range(32))


@pytest.fixture
def fake() -> FakeNextcloud:
    f = FakeNextcloud({USER: PASSWORD})
    f.cal(USER, TEST_CAL)
    f.cal(USER, READ_ONLY, writable=False)
    return f


INTERNAL_FIELDS = {
    "stream_max_seconds",
    "poll_active_seconds",
    "poll_idle_seconds",
    "positive_cache_seconds",
    "failure_limit",
    "event_detail",
    "default_timezone",
    "autodiscover_culture",
    "log_level",
    "log_stdout",
}


def config_for(tmp_path, sections: dict | None = None, **internal):
    """Test configuration from the public schema (`abschnitte` is merged into TESTKONFIG) plus direct overrides of internal fields."""
    k = as_test_accounts(from_dict(mix(TEST_CONFIG, sections or {})))
    assert set(internal) <= INTERNAL_FIELDS, set(internal) - INTERNAL_FIELDS
    return dataclasses.replace(k, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", **{"log_level": "debug", "log_stdout": False, **internal})


async def close(d) -> None:
    d.end.set()
    await d.store.close()
    d.storage.wait_empty()
    d.storage.close()


@pytest.fixture
async def services(fake, clock, tmp_path):
    import_all()
    d = await build_services(config_for(tmp_path), KEY, clock, caldav=fake)
    d.notifier.watch = lambda account: None
    yield d
    await close(d)


@pytest.fixture
async def services_factory(fake, clock, tmp_path):
    """Factory for tests with a different configuration; cleans up at the end."""
    generated = []

    async def build(sections: dict | None = None, **internal):
        import_all()
        d = await build_services(config_for(tmp_path, sections, **internal), KEY, clock, caldav=fake)
        d.notifier.watch = lambda account: None
        generated.append(d)
        return d

    yield build
    for d in generated:
        await close(d)


def session(d, password: str = PASSWORD) -> Session:
    return Session(d.config.accounts[0], Credentials(USER, password), "127.0.0.1")


def context(d, payload: str, zone: str | None = None) -> Context:
    request = soap.read(h.envelope(payload, zone), None)
    return Context(session(d), request, d.config, d.ids, d.store, d.caldav, d.subscriptions, d.notifier, d.locks, d.info, d.clock, d.log, "t" * 12, d.end)


async def ops(d, payload: str, zone: str | None = None):
    """Calls the handler directly (without HTTP). Returns messages, a reply or a stream."""
    ctx = context(d, payload, zone)
    return await OPERATIONS[ctx.request.operation](ctx)


def messages_xml(result) -> list:
    return list(result.messages)


def codes(result) -> list[str]:
    return list(result.codes)


def read_protocol(d) -> list[dict]:
    d.storage.wait_empty()
    lines = []
    for file in sorted(d.config.logs_dir.glob("davbridge-*.jsonl")):
        lines += [json.loads(z) for z in file.read_text().splitlines() if z.strip()]
    return lines


@pytest.fixture
async def bridge(fake, clock, tmp_path):
    """(client, d): HTTP path through ASGITransport, with lifespan."""
    import_all()
    built = {}

    async def factory():
        d = await build_services(config_for(tmp_path), KEY, clock, caldav=fake)
        d.notifier.watch = lambda account: None
        built["d"] = d
        return d

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=h.basic(USER, PASSWORD)) as client:
            yield client, built["d"]


async def post(client, payload: str, zone: str | None = None):
    r = await client.post("/EWS/Exchange.asmx", content=h.envelope(payload, zone))
    assert r.status_code == 200, r.text[:300]
    return h.parse(r.content)


def text(root, name, ns=h.M):
    e = root.find(".//{%s}%s" % (ns, name))
    return e.text if e is not None else None
