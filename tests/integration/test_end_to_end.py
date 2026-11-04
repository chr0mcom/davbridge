"""ASGI app against the real CalDAV server (only the synthetic user). Create -> Sync -> Update -> Delete with stable IDs."""

from __future__ import annotations

import httpx
import pytest

from davbridge.clock import SystemClock
from davbridge.main import build_services, create_app
from tests import soap_helpers as h

pytestmark = pytest.mark.integration
PASS = None


@pytest.fixture
async def bridge(real_config, tmp_path, credentials):
    import dataclasses

    config = dataclasses.replace(real_config, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", poll_active_seconds=1, log_stdout=False)
    holder = {}

    async def factory():
        d = await build_services(config, bytes(range(32)), SystemClock())
        holder["d"] = d
        return d

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=h.basic(credentials.user, credentials.password)) as client:
            yield client, holder["d"]


async def post(client, payload, **kw):
    r = await client.post("/EWS/Exchange.asmx", content=h.envelope(payload, kw.get("zone")))
    assert r.status_code == 200, r.text[:500]
    return h.parse(r.content)


def text(root, name, ns=h.M):
    e = root.find(".//{%s}%s" % (ns, name))
    return e.text if e is not None else None


async def test_folder_and_empty_calendar(bridge):
    client, d = bridge
    w = await post(client, h.get_folder("msgfolderroot", "inbox", "calendar", "archivemsgfolderroot", "contacts"))
    assert h.codes(w) == ["NoError", "NoError", "NoError", "ErrorFolderNotFound", "NoError"]
    names = [e.text for e in w.iter("{%s}DisplayName" % h.T)]
    assert "Test Calendar" in names


async def test_create_sync_update_delete(bridge, credentials):
    client, d = bridge
    cal = d.ids.folder_id(credentials.user, "test-calendar")
    w = await post(client, h.sync_items(cal))
    assert h.codes(w) == ["NoError"]
    state0 = text(w, "SyncState")
    w = await post(client, h.create_item("E2E Test appointment", "2026-12-02T09:00:00Z", "2026-12-02T10:00:00Z", "Room 1"), zone="W. Europe Standard Time")
    assert h.codes(w) == ["NoError"], h.codes(w)
    item = w.find(".//{%s}ItemId" % h.T)
    item_id, ck = item.get("Id"), item.get("ChangeKey")
    try:
        w = await post(client, h.sync_items(cal, state0))
        assert w.find(".//{%s}Create" % h.T) is not None
        state1 = text(w, "SyncState")
        w = await post(client, h.sync_items(cal, state1))
        assert w.find(".//{%s}Create" % h.T) is None and w.find(".//{%s}Update" % h.T) is None
        assert text(w, "SyncState") == state1
        w = await post(client, h.get_item(item_id))
        assert text(w, "Subject", h.T) == "E2E Test appointment" and text(w, "Location", h.T) == "Room 1"
        w = await post(client, h.update_subject(item_id, ck, "E2E changed"))
        assert h.codes(w) == ["NoError"], h.codes(w)
        ck2 = w.find(".//{%s}ItemId" % h.T).get("ChangeKey")
        assert ck2 != ck
        w = await post(client, h.update_subject(item_id, ck, "E2E veraltet"))
        assert h.codes(w) == ["ErrorIrresolvableConflict"]
        w = await post(client, h.update_subject(item_id, None, "E2E ohne ChangeKey"))
        assert h.codes(w) == ["NoError"]
    finally:
        w = await post(client, h.get_item(item_id))
        item = w.find(".//{%s}ItemId" % h.T)
        if item is not None:
            w = await post(client, h.delete_item(item_id, item.get("ChangeKey")))
            assert h.codes(w) == ["NoError"]
    w = await post(client, h.get_item(item_id))
    assert h.codes(w) == ["ErrorItemNotFound"]


async def test_wrong_password_and_foreign_user(bridge, credentials):
    client, d = bridge
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client._transport.app), base_url="http://test") as raw:
        for header in (h.basic(credentials.user, "wrong-wrong"), h.basic("nobody-here", "egal-egal"), {"Content-Type": "text/xml"}):
            r = await raw.post("/EWS/Exchange.asmx", content=h.envelope(h.get_folder("inbox")), headers=header)
            assert r.status_code == 401
