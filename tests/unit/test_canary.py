"""Canary test: no event content, passwords or client identifiers reach stdout, file logs, status.json or SQLite (EWS path, with stdout logging on)."""

import asyncio
import dataclasses
import json

import httpx
from tests import soap_helpers as h
from tests.unit.conftest import KEY, PASSWORD, USER, config_for

from davbridge.main import build_services, create_app
from davbridge.operations import import_all

CANARY = ["CANARY-SUBJECT", "CANARY-LOCATION", "CANARY-BODY", "CANARY-PASSWORD", "CANARY-WRONG", "CANARY-AGENT", "CANARY-USER"]
START, END = "2026-12-01T09:00:00", "2026-12-01T10:00:00"


async def test_canary_no_content_in_stdout_logs_status_and_sqlite(fake, clock, tmp_path, capsys):
    import_all()
    built = {}

    async def factory():
        k = config_for(tmp_path, log_stdout=True)
        k = dataclasses.replace(k, accounts=tuple(dataclasses.replace(x, test=False, capture=False) for x in k.accounts))
        d = await build_services(k, KEY, clock, caldav=fake)
        d.notifier.watch = lambda account: None
        built["d"] = d
        return d

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            login = h.basic(USER, PASSWORD)
            login["User-Agent"] = "CANARY-AGENT/1.0"
            new = h.create_item("CANARY-SUBJECT", START, END, location="CANARY-LOCATION", extra="<t:Body BodyType='Text'>CANARY-BODY</t:Body>")
            r = await client.post("/EWS/Exchange.asmx", content=h.envelope(new, "W. Europe Standard Time"), headers=login)
            assert r.status_code == 200
            await client.post("/EWS/Exchange.asmx", content=h.envelope(h.get_folder("calendar")), headers=login)
            for user, password in (("CANARY-USER", "CANARY-WRONG"), (USER, "CANARY-PASSWORD")):
                wrong = h.basic(user, password)
                wrong["User-Agent"] = "CANARY-AGENT/1.0"
                assert (await client.post("/EWS/Exchange.asmx", content=h.envelope(h.get_folder("calendar")), headers=wrong)).status_code == 401
            await client.get("/CANARY-SUBJECT/ews/CANARY-LOCATION")
        d = built["d"]
        d.storage.wait_empty()
        await d.store.clean_up()
        for _ in range(50):
            if (d.config.state_dir / "status.json").exists():
                break
            await asyncio.sleep(0.02)
        status_raw = (d.config.state_dir / "status.json").read_bytes()
    output = capsys.readouterr()
    assert '"event":"request"' in output.out and '"event":"login_rejected"' in output.out
    finds = [(k, "stdout") for k in CANARY if k in output.out + output.err]
    files = [p for p in list(d.config.state_dir.rglob("*")) + list(d.config.logs_dir.rglob("*")) if p.is_file()]
    assert any(p.suffix == ".jsonl" for p in files) and any(p.suffix == ".sqlite" for p in files)
    finds += [("status.json", k) for k in CANARY if k.encode() in status_raw]
    for file in files:
        raw = file.read_bytes()
        finds += [(file.name, k) for k in CANARY if k.encode() in raw]
    assert finds == []


async def test_status_json_is_removed_on_a_clean_shutdown(fake, clock, tmp_path):
    import_all()
    built = {}

    async def factory():
        d = await build_services(config_for(tmp_path), KEY, clock, caldav=fake)
        built["d"] = d
        return d

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        path = built["d"].config.state_dir / "status.json"
        for _ in range(50):
            if path.exists():
                break
            await asyncio.sleep(0.02)
        assert path.exists()
    assert not path.exists()


async def test_status_json_has_english_keys_and_no_german_ones(fake, clock, tmp_path):
    import_all()
    built = {}

    async def factory():
        d = await build_services(config_for(tmp_path), KEY, clock, caldav=fake)
        built["d"] = d
        return d

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        path = built["d"].config.state_dir / "status.json"
        for _ in range(50):
            if path.exists():
                break
            await asyncio.sleep(0.02)
        status = json.loads(path.read_text())
    assert set(status) == {"time", "version", "commit", "subscriptions", "streams", "last_caldav_ok", "caldav_errors_since", "active_locks", "log_fields_dropped"}
    assert status["version"] == "0.1.0" and status["active_locks"] == 0
