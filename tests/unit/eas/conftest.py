"""Fixtures for the EAS path: DAVbridge with `[protocols.eas] enabled`, a test account with `eas_enabled = true`, a second account without EAS,
requests as WBXML."""

from __future__ import annotations

import base64
import dataclasses

import httpx
import pytest
from tests.conftest import TEST_CONFIG, as_test_accounts, mix

from davbridge.config import from_dict
from davbridge.eas import tags, wbxml
from davbridge.main import build_services, create_app
from davbridge.operations import import_all

PASSWORD = "pw-test-1234"
USER = "test-user"
SECOND = "second"
SECOND_PW = "pw-second-5678"
KEY = bytes(range(32))
DEVICE = "device01"


def eas_config(tmp_path, *, enabled: bool = True, overwrite: dict | None = None):
    raw = mix(TEST_CONFIG, overwrite or {})
    raw["users"][0]["eas_enabled"] = enabled
    raw["users"].append(
        {
            "username": SECOND,
            "address": "second@dav.example.com",
            "display_name": "Second",
            "calendars": [{"path": "second-kal", "id": "second", "display_name": "Second", "default": True, "write": "caldav", "owner": SECOND}],
        }
    )
    raw["auth"]["allowed_users"] = [u["username"] for u in raw["users"]]
    if enabled:
        raw["protocols"] = mix(raw.get("protocols", {}), {"eas": {"enabled": True, "acknowledge_mobile_cloud_processing": True, **raw.get("protocols", {}).get("eas", {})}})
    k = as_test_accounts(from_dict(raw))
    return dataclasses.replace(k, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", log_level="debug", log_stdout=False)


def basic(user: str = USER, password: str = PASSWORD) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


async def _start(fake, clock, tmp_path, enabled: bool, overwrite: dict | None = None):
    import_all()
    fake.passwords[SECOND] = SECOND_PW
    fake.cal(SECOND, "second-kal")
    built = {}

    async def factory():
        d = await build_services(eas_config(tmp_path, enabled=enabled, overwrite=overwrite), KEY, clock, caldav=fake)
        d.notifier.watch = lambda account: None
        built["d"] = d
        return d

    return create_app(factory), built


@pytest.fixture
async def eas_bridge(fake, clock, tmp_path):
    app, built = await _start(fake, clock, tmp_path, True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield EasClient(client, built["d"], fake)


@pytest.fixture
def eas_bridge_with(fake, clock, tmp_path):
    """Factory for EAS tests with a different configuration: `async with eas_bridge_with({...}) as c`."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def build(overwrite: dict):
        app, built = await _start(fake, clock, tmp_path, True, overwrite)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                yield EasClient(client, built["d"], fake)

    return build


@pytest.fixture
async def ews_bridge_from(fake, clock, tmp_path):
    app, built = await _start(fake, clock, tmp_path, False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, built["d"]


class EasClient:
    def __init__(self, http: httpx.AsyncClient, d, fake) -> None:
        self.http = http
        self.d = d
        self.fake = fake

    async def raw(self, command: str, content: bytes | None, *, device: str = DEVICE, version: str = "14.1", header: dict | None = None, user: str = USER, password: str = PASSWORD) -> httpx.Response:
        h = {**basic(user, password), "MS-ASProtocolVersion": version, "User-Agent": "Outlook-iOS/2.0"}
        if content:
            h["Content-Type"] = "application/vnd.ms-sync.wbxml"
        h.update(header or {})
        return await self.http.post(f"/Microsoft-Server-ActiveSync?Cmd={command}&User={user}&DeviceId={device}&DeviceType=iPhone", content=content or b"", headers=h)

    async def command(self, command: str, root: wbxml.Node | None, **kw) -> wbxml.Node | None:
        r = await self.raw(command, wbxml.encode(root) if root is not None else None, **kw)
        assert r.status_code == 200, (r.status_code, r.headers)
        return wbxml.decode(r.content) if r.content else None


def k(qname: str, *children, text=None) -> wbxml.Node:
    return tags.node(qname, *children, text=None if text is None else str(text))


def sync_request(sync_key: str, cid: str, *, window: int | None = None, commands: list | None = None, get_changes: bool | None = None, extra: list | None = None) -> wbxml.Node:
    parts = [k("AirSync:SyncKey", text=sync_key), k("AirSync:CollectionId", text=cid)]
    if get_changes is not None:
        parts.append(k("AirSync:GetChanges", text=1 if get_changes else 0))
    if window is not None:
        parts.append(k("AirSync:WindowSize", text=window))
    parts.append(k("AirSync:Options", k("AirSyncBase:BodyPreference", k("AirSyncBase:Type", text=1), k("AirSyncBase:TruncationSize", text=32768))))
    if commands:
        parts.append(k("AirSync:Commands", *commands))
    return k("AirSync:Sync", k("AirSync:Collections", k("AirSync:Collection", *parts)), *(extra or []))


def collection(response: wbxml.Node, index: int = 0) -> wbxml.Node:
    return tags.find_all(tags.find(response, "AirSync:Collections"), "AirSync:Collection")[index]


def status(node: wbxml.Node, qname: str) -> int:
    return int(tags.text_from(node, qname))
