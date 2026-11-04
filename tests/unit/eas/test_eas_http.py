"""EAS path end to end over ASGI with FakeNextcloud (HTTP, commands, mobile autodiscover, authentication)."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import struct

import pytest
from tests.doubles.fake_nextcloud import ics, series_ics
from tests.unit.eas.conftest import PASSWORD, SECOND, SECOND_PW, USER, basic, collection, k, status, sync_request

from davbridge.eas import tags, timezone, wbxml

TEST_CAL_URI = "test-calendar"
READ_ONLY_URI = "read-only-calendar_shared_by_read-only-owner"
BERLIN = timezone.blob_base64("Europe/Berlin", dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc))


def appointment_data(title: str, *, start="20261210T090000Z", end="20261210T100000Z", uid: str | None = None, extra=(), reminder=15, location: str | None = None) -> wbxml.Node:
    parts = [
        k("Calendar:Timezone", text=BERLIN),
        k("Calendar:AllDayEvent", text=0),
        k("Calendar:BusyStatus", text=2),
        k("Calendar:EndTime", text=end),
        k("Calendar:Sensitivity", text=0),
        k("Calendar:Subject", text=title),
        k("Calendar:StartTime", text=start),
    ]
    if reminder is not None:
        parts.append(k("Calendar:Reminder", text=reminder))
    if location is not None:
        parts.append(k("Calendar:Location", text=location))
    if uid:
        parts.append(k("Calendar:UID", text=uid))
    parts.extend(extra)
    return k("AirSync:ApplicationData", *parts)


async def first_sync(c, cid="test-calendar", window=100):
    a = await c.command("Sync", sync_request("0", cid))
    sk = tags.text_from(collection(a), "AirSync:SyncKey")
    a = await c.command("Sync", sync_request(sk, cid, window=window))
    return a


def elements(a, kind="AirSync:Add"):
    cmds = tags.find(collection(a), "AirSync:Commands")
    return tags.find_all(cmds, kind) if cmds is not None else ()


def key(a):
    return tags.text_from(collection(a), "AirSync:SyncKey")


# ------------------------------------------------------------------ HTTP frame


async def test_options_needs_basic_and_names_version_and_commands(eas_bridge):
    r = await eas_bridge.http.options("/Microsoft-Server-ActiveSync")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic ")
    assert "bearer" not in str(r.headers).lower()
    r = await eas_bridge.http.options("/Microsoft-Server-ActiveSync", headers=basic())
    assert r.status_code == 200
    assert r.headers["ms-asprotocolversions"] == "14.1"
    commands = r.headers["ms-asprotocolcommands"].split(",")
    assert len(commands) == 17 and "Sync" in commands and "Ping" in commands and "Find" not in commands


@pytest.mark.parametrize("path", ["/Microsoft-Server-ActiveSync/", "/microsoft-server-activesync", "/MICROSOFT-SERVER-ACTIVESYNC", "/Microsoft-Server-Activesync/", "/microsoft-server-activesync//"])
async def test_options_works_on_the_case_insensitive_path_with_an_optional_trailing_slash(eas_bridge, path):
    assert (await eas_bridge.http.options(path)).status_code == 401
    r = await eas_bridge.http.options(path, headers=basic())
    assert r.status_code == 200 and r.headers["ms-asprotocolversions"] == "14.1"


async def test_options_on_other_paths_is_still_not_served(eas_bridge):
    for path in ("/something-else", "/EWS/Exchange.asmx", "/healthz", "/microsoft-server-activesync/extra"):
        r = await eas_bridge.http.options(path, headers=basic())
        assert r.status_code == 405 and r.headers["allow"] == "GET, POST"


async def test_account_without_eas_gets_403_after_login(eas_bridge):
    r = await eas_bridge.raw("FolderSync", wbxml.encode(k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=0))), user=SECOND, password=SECOND_PW)
    assert r.status_code == 403
    r = await eas_bridge.raw("FolderSync", b"\x03", user=SECOND, password="wrong-password")
    assert r.status_code == 401


@pytest.mark.parametrize(
    "command,version,content,expected",
    [
        ("FolderSync", "16.1", wbxml.encode(k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=0))), 400),
        ("GetAttachment", "14.1", None, 501),
        ("Find", "14.1", None, 501),
        ("Unsinn", "14.1", None, 400),
        ("FolderSync", "14.1", b"\x03\x01\x6a\x00\x45\x02", 400),
        ("FolderSync", "14.1", None, 400),
    ],
)
async def test_form_error(eas_bridge, command, version, content, expected):
    r = await eas_bridge.raw(command, content, version=version)
    assert r.status_code == expected


async def test_wrong_content_type_and_too_large(eas_bridge):
    content = wbxml.encode(k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=0)))
    r = await eas_bridge.raw("FolderSync", content, header={"Content-Type": "text/xml"})
    assert r.status_code == 400
    r = await eas_bridge.raw("FolderSync", b"\x03" * (600 * 1024))
    assert r.status_code == 413


async def test_base64_query(eas_bridge):
    device = b"abc123"
    type = b"iPhone"
    raw = bytes([141, 9]) + struct.pack("<H", 0x0407) + bytes([len(device)]) + device + b"\x00" + bytes([len(type)]) + type
    query = base64.b64encode(raw).decode()
    content = wbxml.encode(k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=0)))
    r = await eas_bridge.http.post(f"/Microsoft-Server-ActiveSync?{query}", content=content, headers={**basic(), "Content-Type": "application/vnd.ms-sync.wbxml"})
    assert r.status_code == 200
    assert status(wbxml.decode(r.content), "FolderHierarchy:Status") == 1


async def test_switch_off_is_todays_state(ews_bridge_from):
    client, _ = ews_bridge_from
    r = await client.post("/Microsoft-Server-ActiveSync?Cmd=FolderSync&User=x&DeviceId=a&DeviceType=b", headers=basic())
    assert r.status_code == 404
    r = await client.get("/autodiscover/autodiscover.json?Email=a@dav.example.com&Protocol=ActiveSync")
    assert r.status_code == 404
    r = await client.options("/EWS/Exchange.asmx")
    assert r.status_code == 405
    r = await client.post("/autodiscover/autodiscover.xml", content=b"<Autodiscover xmlns='http://schemas.microsoft.com/exchange/autodiscover/mobilesync/requestschema/2006'/>")
    assert r.status_code == 401


async def test_ews_path_stays_with_eas_enabled(eas_bridge):
    r = await eas_bridge.http.options("/EWS/Exchange.asmx")
    assert r.status_code == 405
    r = await eas_bridge.http.post("/autodiscover/autodiscover.xml", content=b"<Autodiscover/>")
    assert r.status_code == 401


# ------------------------------------------------------------------ Provision, FolderSync, Settings


async def test_provision_two_phase(eas_bridge):
    a = await eas_bridge.command("Provision", k("Provision:Provision", k("Provision:Policies", k("Provision:Policy", k("Provision:PolicyType", text="MS-EAS-Provisioning-WBXML")))))
    policy = tags.path(a, "Provision:Policies", "Provision:Policy")
    assert status(a, "Provision:Status") == 1 and status(policy, "Provision:Status") == 1
    doc = tags.path(policy, "Provision:Data", "Provision:EASProvisionDoc")
    assert tags.text_from(doc, "Provision:DevicePasswordEnabled") == "0"
    temp = tags.text_from(policy, "Provision:PolicyKey")
    a = await eas_bridge.command(
        "Provision",
        k(
            "Provision:Provision",
            k("Provision:Policies", k("Provision:Policy", k("Provision:PolicyType", text="MS-EAS-Provisioning-WBXML"), k("Provision:PolicyKey", text=temp), k("Provision:Status", text=1))),
        ),
    )
    policy = tags.path(a, "Provision:Policies", "Provision:Policy")
    assert tags.text_from(policy, "Provision:PolicyKey") not in (None, temp)
    assert tags.find(policy, "Provision:Data") is None


async def test_foldersync_chain(eas_bridge):
    a = await eas_bridge.command("FolderSync", k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=0)))
    changes = tags.find(a, "FolderHierarchy:Changes")
    assert tags.text_from(changes, "FolderHierarchy:Count") == "9"
    types = {tags.text_from(x, "FolderHierarchy:ServerId"): tags.text_from(x, "FolderHierarchy:Type") for x in tags.find_all(changes, "FolderHierarchy:Add")}
    assert types["test-calendar"] == "8" and types["read-only-calendar"] == "13" and types["inbox"] == "2"
    sk = tags.text_from(a, "FolderHierarchy:SyncKey")
    assert len(sk) <= 64
    a = await eas_bridge.command("FolderSync", k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=sk)))
    assert tags.text_from(tags.find(a, "FolderHierarchy:Changes"), "FolderHierarchy:Count") == "0"
    a = await eas_bridge.command("FolderSync", k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=sk)), device="anderes")
    assert status(a, "FolderHierarchy:Status") == 9


async def test_settings(eas_bridge):
    a = await eas_bridge.command(
        "Settings",
        k(
            "Settings:Settings",
            k("Settings:Oof", k("Settings:Get", k("Settings:BodyType", text="Text"))),
            k("Settings:DeviceInformation", k("Settings:Set", k("Settings:Model", text="iPhone15,3"), k("Settings:IMEI", text="CANARY-IMEI"))),
            k("Settings:UserInformation", k("Settings:Get")),
        ),
    )
    assert status(a, "Settings:Status") == 1
    account = tags.path(a, "Settings:UserInformation", "Settings:Get", "Settings:Accounts", "Settings:Account")
    assert tags.text_from(account, "Settings:SendDisabled") == "1"
    a = await eas_bridge.command("Settings", k("Settings:Settings", k("Settings:Oof", k("Settings:Set", k("Settings:OofState", text=1)))))
    assert status(a, "Settings:Status") == 7


# ------------------------------------------------------------------ Sync


async def test_sync_paging_all_exactly_once(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    for i in range(10):
        cal.write(f"t{i}.ics", ics(f"uid-{i}", f"Appointment {i}"))
    a = await eas_bridge.command("Sync", sync_request("0", "test-calendar"))
    assert status(collection(a), "AirSync:Status") == 1 and elements(a) == ()
    sk = key(a)
    seen = []
    for _ in range(6):
        a = await eas_bridge.command("Sync", sync_request(sk, "test-calendar", window=3))
        seen += [tags.text_from(x, "AirSync:ServerId") for x in elements(a)]
        sk = key(a)
        if tags.find(collection(a), "AirSync:MoreAvailable") is None:
            break
    assert len(seen) == 10 and len(set(seen)) == 10
    assert await eas_bridge.command("Sync", sync_request(sk, "test-calendar")) is None


async def test_sync_get_changes_at_key_0_is_protocol_error(eas_bridge):
    a = await eas_bridge.command("Sync", sync_request("0", "test-calendar", get_changes=True))
    assert status(collection(a), "AirSync:Status") == 4


async def test_unknown_folder_status_12(eas_bridge):
    a = await eas_bridge.command("Sync", sync_request("0", "does-not-exist"))
    assert status(collection(a), "AirSync:Status") == 12


async def test_retry_yields_the_same_response_without_a_second_appointment(eas_bridge):
    a = await eas_bridge.command("Sync", sync_request("0", "test-calendar"))
    sk = key(a)
    request = sync_request(sk, "test-calendar", commands=[k("AirSync:Add", k("AirSync:ClientId", text="c1"), appointment_data("M-Wiederholung"))])
    first = await eas_bridge.command("Sync", request)
    second = await eas_bridge.command("Sync", request)
    assert second == first
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    assert sum("M-Wiederholung" in i for _, i in cal.dav_objects.values()) == 1
    response_add = tags.find(tags.find(collection(first), "AirSync:Responses"), "AirSync:Add")
    assert status(response_add, "AirSync:Status") == 1 and tags.text_from(response_add, "AirSync:ClientId") == "c1"
    otherwise = sync_request(sk, "test-calendar")
    assert status(collection(await eas_bridge.command("Sync", otherwise)), "AirSync:Status") == 3


async def test_add_change_delete_and_no_echo(eas_bridge):
    a = await eas_bridge.command("Sync", sync_request("0", "test-calendar"))
    a = await eas_bridge.command("Sync", sync_request(key(a), "test-calendar", commands=[k("AirSync:Add", k("AirSync:ClientId", text="c1"), appointment_data("DAVBRIDGE-TEST-1", uid="ios-uid-1"))]))
    sid = tags.text_from(tags.find(tags.find(collection(a), "AirSync:Responses"), "AirSync:Add"), "AirSync:ServerId")
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    href, (etag, text) = next((h, v) for h, v in cal.dav_objects.items() if "DAVBRIDGE-TEST-1" in v[1])
    assert "X-DAVBRIDGE-CREATED-BY:outlook" in text and "ATTENDEE" not in text and "UID:ios-uid-1" in text
    sk = key(a)
    assert await eas_bridge.command("Sync", sync_request(sk, "test-calendar")) is None
    a = await eas_bridge.command("Sync", sync_request(sk, "test-calendar", commands=[k("AirSync:Change", k("AirSync:ServerId", text=sid), appointment_data("DAVBRIDGE-TEST-1 changed"))]))
    assert "DAVBRIDGE-TEST-1 changed" in cal.dav_objects[href][1]
    a = await eas_bridge.command("Sync", sync_request(key(a), "test-calendar", commands=[k("AirSync:Delete", k("AirSync:ServerId", text=sid))]))
    assert href not in cal.dav_objects


async def test_conflict_nextcloud_wins(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    cal.write("k.ics", ics("uid-k", "Original"))
    a = await first_sync(eas_bridge)
    sid = tags.text_from(elements(a)[0], "AirSync:ServerId")
    cal.write("k.ics", ics("uid-k", "External writer has changed", extra="X-EXAMPLE-PROTECTED-CREATED-BY:external-writer"))
    a = await eas_bridge.command("Sync", sync_request(key(a), "test-calendar", commands=[k("AirSync:Change", k("AirSync:ServerId", text=sid), appointment_data("iPhone will"))]))
    resp = tags.find(tags.find(collection(a), "AirSync:Responses"), "AirSync:Change")
    assert status(resp, "AirSync:Status") == 7
    delivered = elements(a, "AirSync:Change")
    assert len(delivered) == 1 and tags.text_from(tags.find(delivered[0], "AirSync:ApplicationData"), "Calendar:Subject") == "External writer has changed"
    assert "External writer has changed" in cal.dav_objects["k.ics"][1]


async def test_read_only_calendar(eas_bridge):
    cal = eas_bridge.fake.cal(USER, READ_ONLY_URI, writable=False)
    cal.write("g.ics", ics("uid-g", "Imported"))
    a = await first_sync(eas_bridge, "read-only-calendar")
    sid = tags.text_from(elements(a)[0], "AirSync:ServerId")
    a = await eas_bridge.command(
        "Sync",
        sync_request(
            key(a),
            "read-only-calendar",
            commands=[
                k("AirSync:Add", k("AirSync:ClientId", text="c9"), appointment_data("nein")),
                k("AirSync:Change", k("AirSync:ServerId", text=sid), appointment_data("nein")),
            ],
        ),
    )
    responses = tags.find(collection(a), "AirSync:Responses")
    assert status(tags.find(responses, "AirSync:Add"), "AirSync:Status") == 6
    assert status(tags.find(responses, "AirSync:Change"), "AirSync:Status") == 7
    a = await eas_bridge.command("Sync", sync_request(key(a), "read-only-calendar", commands=[k("AirSync:Delete", k("AirSync:ServerId", text=sid))]))
    assert [tags.text_from(x, "AirSync:ServerId") for x in elements(a)] == [sid]
    assert "g.ics" in cal.dav_objects


async def test_series_readable_but_not_changeable(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    cal.write("s.ics", series_ics("uid-s"))
    a = await first_sync(eas_bridge)
    adds = elements(a)
    assert len(adds) == 1
    assert tags.find(tags.find(adds[0], "AirSync:ApplicationData"), "Calendar:Recurrence") is not None
    sid = tags.text_from(adds[0], "AirSync:ServerId")
    a = await eas_bridge.command("Sync", sync_request(key(a), "test-calendar", commands=[k("AirSync:Change", k("AirSync:ServerId", text=sid), appointment_data("nein"))]))
    assert status(tags.find(tags.find(collection(a), "AirSync:Responses"), "AirSync:Change"), "AirSync:Status") == 7
    assert "RRULE" in cal.dav_objects["s.ics"][1]


async def test_mail_folder_empty_and_add_rejected(eas_bridge):
    a = await eas_bridge.command("Sync", sync_request("0", "inbox"))
    a = await eas_bridge.command("Sync", sync_request(key(a), "inbox", commands=[k("AirSync:Add", k("AirSync:ClientId", text="m1"), k("AirSync:ApplicationData"))]))
    assert status(tags.find(tags.find(collection(a), "AirSync:Responses"), "AirSync:Add"), "AirSync:Status") == 6
    assert elements(a) == ()


async def test_forged_and_foreign_key(eas_bridge):
    a = await eas_bridge.command("Sync", sync_request("0", "test-calendar"))
    sk = key(a)
    assert status(collection(await eas_bridge.command("Sync", sync_request(sk[:-2] + "AA", "test-calendar"))), "AirSync:Status") == 3
    assert status(collection(await eas_bridge.command("Sync", sync_request(sk, "test-calendar"), device="foreign")), "AirSync:Status") == 3
    assert status(collection(await eas_bridge.command("Sync", sync_request(sk, "read-only-calendar"))), "AirSync:Status") == 3


async def test_empty_sync_without_template_13_with_template_as_last(eas_bridge):
    r = await eas_bridge.raw("Sync", None)
    assert status(wbxml.decode(r.content), "AirSync:Status") == 13
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    a = await eas_bridge.command("Sync", sync_request("0", "test-calendar"))
    assert await eas_bridge.command("Sync", sync_request(key(a), "test-calendar")) is None
    r = await eas_bridge.raw("Sync", None)
    assert r.status_code == 200 and r.content == b""
    cal.write("leer.ics", ics("uid-leer"))
    r = await eas_bridge.raw("Sync", None)
    assert len(elements(wbxml.decode(r.content))) == 1


async def test_wait_and_heartbeat_limits(eas_bridge):
    both = sync_request("0", "test-calendar", extra=[k("AirSync:Wait", text=5), k("AirSync:HeartbeatInterval", text=120)])
    assert status(await eas_bridge.command("Sync", both), "AirSync:Status") == 4
    too_long = sync_request("0", "test-calendar", extra=[k("AirSync:Wait", text=59)])
    a = await eas_bridge.command("Sync", too_long)
    assert status(a, "AirSync:Status") == 14 and tags.text_from(a, "AirSync:Limit") == "9"


async def test_get_item_estimate(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    for i in range(4):
        cal.write(f"e{i}.ics", ics(f"uid-e{i}"))

    def estimate(sk):
        return k(
            "GetItemEstimate:GetItemEstimate", k("GetItemEstimate:Collections", k("GetItemEstimate:Collection", k("AirSync:SyncKey", text=sk), k("GetItemEstimate:CollectionId", text="test-calendar")))
        )

    a = await eas_bridge.command("GetItemEstimate", estimate("0"))
    assert status(tags.find(a, "GetItemEstimate:Response"), "GetItemEstimate:Status") == 3
    s = await eas_bridge.command("Sync", sync_request("0", "test-calendar"))
    a = await eas_bridge.command("GetItemEstimate", estimate(key(s)))
    resp = tags.find(a, "GetItemEstimate:Response")
    assert status(resp, "GetItemEstimate:Status") == 1
    assert tags.text_from(tags.find(resp, "GetItemEstimate:Collection"), "GetItemEstimate:Estimate") == "4"


async def test_item_operations_fetch(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    cal.write("f.ics", ics("uid-f", "Abruf"))
    a = await first_sync(eas_bridge)
    sid = tags.text_from(elements(a)[0], "AirSync:ServerId")
    a = await eas_bridge.command(
        "ItemOperations",
        k("ItemOperations:ItemOperations", k("ItemOperations:Fetch", k("ItemOperations:Store", text="Mailbox"), k("AirSync:CollectionId", text="test-calendar"), k("AirSync:ServerId", text=sid))),
    )
    fetch = tags.path(a, "ItemOperations:Response", "ItemOperations:Fetch")
    assert status(fetch, "ItemOperations:Status") == 1
    assert tags.text_from(tags.find(fetch, "ItemOperations:Properties"), "Calendar:Subject") == "Abruf"
    a = await eas_bridge.command(
        "ItemOperations",
        k(
            "ItemOperations:ItemOperations",
            k("ItemOperations:Fetch", k("ItemOperations:Store", text="Mailbox"), k("AirSync:CollectionId", text="test-calendar"), k("AirSync:ServerId", text="000000000000")),
        ),
    )
    assert status(tags.path(a, "ItemOperations:Response", "ItemOperations:Fetch"), "ItemOperations:Status") == 6


@pytest.mark.parametrize(
    "command,root,status_path,expected",
    [
        ("SendMail", k("ComposeMail:SendMail", k("ComposeMail:ClientId", text="x")), ("ComposeMail:Status",), 167),
        ("SmartReply", k("ComposeMail:SmartReply"), ("ComposeMail:Status",), 167),
        ("MeetingResponse", k("MeetingResponse:MeetingResponse", k("MeetingResponse:Request", k("MeetingResponse:RequestId", text="r1"))), ("MeetingResponse:Result", "MeetingResponse:Status"), 2),
        ("MoveItems", k("Move:MoveItems", k("Move:Move", k("Move:SrcMsgId", text="a"))), ("Move:Response", "Move:Status"), 2),
        ("FolderCreate", k("FolderHierarchy:FolderCreate"), ("FolderHierarchy:Status",), 10),
        ("FolderDelete", k("FolderHierarchy:FolderDelete"), ("FolderHierarchy:Status",), 3),
        ("FolderUpdate", k("FolderHierarchy:FolderUpdate"), ("FolderHierarchy:Status",), 2),
        ("Search", k("Search:Search"), ("Search:Status",), 1),
        ("ResolveRecipients", k("ResolveRecipients:ResolveRecipients", k("ResolveRecipients:To", text="a@b.c")), ("ResolveRecipients:Status",), 1),
    ],
)
async def test_rejected_commands(eas_bridge, command, root, status_path, expected):
    a = await eas_bridge.command(command, root)
    target = tags.path(a, *status_path[:-1]) if len(status_path) > 1 else a
    assert status(target, status_path[-1]) == expected


# ------------------------------------------------------------------ Ping


def ping(hb=None, *folder):
    parts = []
    if hb is not None:
        parts.append(k("Ping:HeartbeatInterval", text=hb))
    if folder:
        parts.append(k("Ping:Folders", *(k("Ping:Folder", k("Ping:Id", text=o), k("Ping:Class", text="Calendar")) for o in folder)))
    return k("Ping:Ping", *parts)


async def test_ping_parameter(eas_bridge):
    assert status(await eas_bridge.command("Ping", ping()), "Ping:Status") == 3
    a = await eas_bridge.command("Ping", ping(30, "test-calendar"))
    assert status(a, "Ping:Status") == 5 and tags.text_from(a, "Ping:HeartbeatInterval") == "60"
    a = await eas_bridge.command("Ping", ping(900, "test-calendar"))
    assert tags.text_from(a, "Ping:HeartbeatInterval") == "540"
    assert status(await eas_bridge.command("Ping", ping(60, "does-not-exist")), "Ping:Status") == 7


async def test_ping_immediate_check_and_wake_call(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    await first_sync(eas_bridge)
    cal.write("neu.ics", ics("uid-neu"))
    a = await eas_bridge.command("Ping", ping(60, "test-calendar"))
    assert status(a, "Ping:Status") == 2
    assert tags.text_from(tags.find(a, "Ping:Folders"), "Ping:Folder") == "test-calendar"
    await eas_bridge.command("Sync", sync_request(key(await eas_bridge.command("Sync", sync_request("0", "test-calendar"))), "test-calendar"))
    task = asyncio.create_task(eas_bridge.command("Ping", ping(60, "test-calendar")))
    await asyncio.sleep(0.2)
    assert not task.done()
    cal.write("noch.ics", ics("uid-noch"))
    eas_bridge.d.eas.waker.report(USER, "test-calendar")
    a = await asyncio.wait_for(task, 5)
    assert status(a, "Ping:Status") == 2


async def test_ping_is_replaced_by_newer_one(eas_bridge):
    await first_sync(eas_bridge)
    first = asyncio.create_task(eas_bridge.command("Ping", ping(60, "test-calendar")))
    await asyncio.sleep(0.2)
    second = asyncio.create_task(eas_bridge.command("Ping", ping(60, "test-calendar")))
    a = await asyncio.wait_for(first, 5)
    assert status(a, "Ping:Status") == 1
    eas_bridge.d.eas.waker.end()
    assert status(await asyncio.wait_for(second, 5), "Ping:Status") == 1


# ------------------------------------------------------------------ Autodiscover (E51)


async def test_autodiscover_json_anonymous_account_neutral(eas_bridge):
    h = eas_bridge.http
    a = await h.get("/autodiscover/autodiscover.json?Email=does-not-exist@dav.example.com&Protocol=ActiveSync")
    b = await h.get("/autodiscover/autodiscover.json/v1.0/test-user@dav.example.com?Protocol=ActiveSync")
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    assert a.json() == {"Protocol": "ActiveSync", "Url": "https://dav.example.com/Microsoft-Server-ActiveSync"}
    assert "www-authenticate" not in a.headers
    v1 = await h.get("/autodiscover/autodiscover.json?Email=x@dav.example.com&Protocol=AutodiscoverV1")
    assert v1.json()["Protocol"] == "AutodiscoverV1"
    assert (await h.get("/autodiscover/autodiscover.json?Email=x@dav.example.com&Protocol=Rest")).status_code == 400
    assert (await h.get("/autodiscover/autodiscover.json?Email=x@foreign.example&Protocol=ActiveSync")).status_code == 404
    host = await h.get("/autodiscover/autodiscover.json?Email=x@dav.example.com&Protocol=ActiveSync", headers={"Host": "boese.example"})
    assert "boese" not in host.text


async def test_autodiscover_pox_mobilesync_anonymous(eas_bridge):
    body = (
        b'<?xml version="1.0"?><Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/mobilesync/requestschema/2006">'
        b"<Request><EMailAddress>test-user@dav.example.com</EMailAddress><AcceptableResponseSchema>"
        b"http://schemas.microsoft.com/exchange/autodiscover/mobilesync/responseschema/2006</AcceptableResponseSchema></Request></Autodiscover>"
    )
    r = await eas_bridge.http.post("/autodiscover/autodiscover.xml", content=body)
    assert r.status_code == 200
    assert b"<Url>https://dav.example.com/Microsoft-Server-ActiveSync</Url>" in r.content and b"MobileSync" in r.content
    r = await eas_bridge.http.post("/autodiscover/autodiscover.xml", content=b"<Autodiscover/>")
    assert r.status_code == 401


# ------------------------------------------------------------------ locks


async def test_eas_lock_503_and_mac_untouched(eas_bridge):
    content = wbxml.encode(k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=0)))
    for _ in range(10):
        r = await eas_bridge.raw("FolderSync", content, password="wrong-password")
        assert r.status_code == 401
    r = await eas_bridge.raw("FolderSync", content)
    assert r.status_code == 503 and 0 < int(r.headers["retry-after"]) <= 900
    from tests import soap_helpers as h

    r = await eas_bridge.http.post("/EWS/Exchange.asmx", content=h.envelope(h.get_folder("calendar")), headers=h.basic(USER, PASSWORD))
    assert r.status_code == 200


async def test_account_throttle_one_check_per_minute_new_password_passes(eas_bridge, clock):
    from davbridge.auth import LoginRejected

    content = wbxml.encode(k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=0)))
    assert (await eas_bridge.raw("FolderSync", content)).status_code == 200
    d = eas_bridge.d
    for i in range(20):
        try:
            await d.login.check(basic(USER, f"wrong-{i}")["Authorization"], f"10.0.0.{i}", namespace="eas:", account_throttle=True)
        except LoginRejected:
            pass
    assert await d.store.lock_check(f"k:{USER}") > 0
    before = eas_bridge.fake.access_calls
    with pytest.raises(LoginRejected):
        await d.login.check(basic(USER, "still-wrong")["Authorization"], "10.0.1.1", namespace="eas:", account_throttle=True)
    assert eas_bridge.fake.access_calls == before + 1
    with pytest.raises(LoginRejected) as e:
        await d.login.check(basic(USER, "wrong-again")["Authorization"], "10.0.1.2", namespace="eas:", account_throttle=True)
    assert e.value.reason == "throttled" and eas_bridge.fake.access_calls == before + 1
    eas_bridge.fake.passwords[USER] = "new-app-password"
    clock.before(61)
    session = await d.login.check(basic(USER, "new-app-password")["Authorization"], "10.0.1.3", namespace="eas:", account_throttle=True)
    assert session.account.name == USER


async def test_two_app_passwords_revoking_one_leaves_the_other(eas_bridge):
    d = eas_bridge.d
    eas_bridge.fake.passwords[USER] = PASSWORD
    await d.login.check(basic(USER, PASSWORD)["Authorization"], "10.0.0.1")
    s2 = await d.login.check(basic(USER, PASSWORD)["Authorization"], "10.0.0.2", namespace="eas:")
    from davbridge.caldav import Credentials

    second = Credentials(USER, "iphone-password")
    d.login._remember_access(second)
    assert d.login.access_for(USER) == second
    d.login.forget(USER, second)
    assert d.login.access_for(USER).password == PASSWORD
    assert s2.account.name == USER


# ------------------------------------------------------------------ Kanarientest (E48)


async def test_canary_no_content_in_sqlite_and_logs(eas_bridge, tmp_path):
    canary = ["CANARY-SUBJECT", "CANARY-LOCATION", "CANARY-BODY", "CANARY-CLIENT", "CANARYDEVICE01", "CANARY-IMEI", "CANARY-TEL"]
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    cal.write("x.ics", ics("uid-x", "CANARY-SUBJECT", extra="LOCATION:CANARY-LOCATION\r\nDESCRIPTION:CANARY-BODY"))
    device = "CANARYDEVICE01"
    await eas_bridge.command(
        "Settings", k("Settings:Settings", k("Settings:DeviceInformation", k("Settings:Set", k("Settings:IMEI", text="CANARY-IMEI"), k("Settings:PhoneNumber", text="CANARY-TEL")))), device=device
    )
    a = await eas_bridge.command("Sync", sync_request("0", "test-calendar"), device=device)
    a = await eas_bridge.command("Sync", sync_request(key(a), "test-calendar"), device=device)
    a = await eas_bridge.command(
        "Sync", sync_request(key(a), "test-calendar", commands=[k("AirSync:Add", k("AirSync:ClientId", text="CANARY-CLIENT"), appointment_data("CANARY-SUBJECT neu"))]), device=device
    )
    await eas_bridge.command("Ping", ping(30, "test-calendar"), device=device)
    d = eas_bridge.d
    d.storage.wait_empty()
    await d.store.clean_up()
    files = list(d.config.state_dir.rglob("*")) + list(d.config.logs_dir.rglob("*"))
    finds = []
    for file in files:
        if file.is_file():
            raw = file.read_bytes()
            for kan in canary:
                if kan.encode() in raw:
                    finds.append((file.name, kan))
    assert finds == []


async def test_truncated_body_echo_does_not_overwrite_description(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    long = "Line " * 2000
    cal.write("b.ics", ics("uid-b", "With text", extra=f"DESCRIPTION:{long.strip()}\r\nX-EXAMPLE-PROTECTED-CREATED-BY:external-writer"))
    a = await eas_bridge.command("Sync", sync_request("0", "test-calendar"))
    request = sync_request(key(a), "test-calendar")
    opt = tags.find(collection(request), "AirSync:Options")
    assert opt is not None
    a = await eas_bridge.command(
        "Sync",
        k(
            "AirSync:Sync",
            k(
                "AirSync:Collections",
                k(
                    "AirSync:Collection",
                    k("AirSync:SyncKey", text=key(a)),
                    k("AirSync:CollectionId", text="test-calendar"),
                    k("AirSync:Options", k("AirSyncBase:BodyPreference", k("AirSyncBase:Type", text=1), k("AirSyncBase:TruncationSize", text=500))),
                ),
            ),
        ),
    )
    element = elements(a)[0]
    body = tags.find(tags.find(element, "AirSync:ApplicationData"), "AirSyncBase:Body")
    assert tags.text_from(body, "AirSyncBase:Truncated") == "1"
    truncated = tags.text_from(body, "AirSyncBase:Data")
    sid = tags.text_from(element, "AirSync:ServerId")
    back = appointment_data(
        "With text, new Title", start="20261201T090000Z", end="20261201T100000Z", reminder=None, extra=[k("AirSyncBase:Body", k("AirSyncBase:Type", text=1), k("AirSyncBase:Data", text=truncated))]
    )
    req = sync_request(key(a), "test-calendar", commands=[k("AirSync:Change", k("AirSync:ServerId", text=sid), back)])
    col = collection(req)
    without_opt = [x for x in col.children if tags.display(x) != "AirSync:Options"]
    opt = k("AirSync:Options", k("AirSyncBase:BodyPreference", k("AirSyncBase:Type", text=1), k("AirSyncBase:TruncationSize", text=500)))
    req = k("AirSync:Sync", k("AirSync:Collections", k("AirSync:Collection", *without_opt[:3], opt, *without_opt[3:])))
    await eas_bridge.command("Sync", req)
    text = cal.dav_objects["b.ics"][1]
    assert "new Title" in text and "X-EXAMPLE-PROTECTED-CREATED-BY:external-writer" in text
    assert text.replace("\r\n ", "").count("Line") == 2000


# ------------------------------------------------------------------ corrections found in review (deletion of missing fields, empty Sync, retry, ...)


@pytest.mark.parametrize("command", ["Sync", "FolderSync", "Ping", "Provision", "Settings", "SendMail"])
async def test_anonymous_post_gets_401_basic_before_any_evaluation(eas_bridge, command):
    r = await eas_bridge.http.post(
        f"/Microsoft-Server-ActiveSync?Cmd={command}&User=x&DeviceId=abc&DeviceType=Outlook",
        content=b"\x03\x01\x6a\x00\x45\x01",
        headers={"Content-Type": "application/vnd.ms-sync.wbxml", "MS-ASProtocolVersion": "14.1"},
    )
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic ")
    assert await eas_bridge.d.store.locks_active() == 0


async def test_provision_order_and_wrong_key(eas_bridge):
    a = await eas_bridge.command(
        "Provision",
        k(
            "Provision:Provision",
            k("Settings:DeviceInformation", k("Settings:Set", k("Settings:Model", text="iPhone"))),
            k("Provision:Policies", k("Provision:Policy", k("Provision:PolicyType", text="MS-EAS-Provisioning-WBXML"))),
        ),
    )
    assert [tags.display(x) for x in a.children] == ["Settings:DeviceInformation", "Provision:Status", "Provision:Policies"]
    doc = tags.path(a, "Provision:Policies", "Provision:Policy", "Provision:Data", "Provision:EASProvisionDoc")
    assert [tags.display(x).split(":")[1] for x in doc.children] == [
        "DevicePasswordEnabled",
        "AlphanumericDevicePasswordRequired",
        "PasswordRecoveryEnabled",
        "AttachmentsEnabled",
        "AllowSimpleDevicePassword",
        "AllowCamera",
        "RequireDeviceEncryption",
        "AllowHTMLEmail",
        "AllowBrowser",
    ]
    a = await eas_bridge.command(
        "Provision",
        k(
            "Provision:Provision",
            k("Provision:Policies", k("Provision:Policy", k("Provision:PolicyType", text="MS-EAS-Provisioning-WBXML"), k("Provision:PolicyKey", text="123"), k("Provision:Status", text=1))),
        ),
    )
    assert status(tags.path(a, "Provision:Policies", "Provision:Policy"), "Provision:Status") == 5


async def test_retry_with_smaller_window_is_no_status_3(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    for i in range(6):
        cal.write(f"w{i}.ics", ics(f"uid-w{i}"))
    a = await eas_bridge.command("Sync", sync_request("0", "test-calendar"))
    sk = key(a)
    first = await eas_bridge.command("Sync", sync_request(sk, "test-calendar", window=4))
    second = await eas_bridge.command("Sync", sync_request(sk, "test-calendar", window=2))
    assert status(collection(second), "AirSync:Status") == 1 and key(second) == key(first)


async def test_ghosting_missing_location_is_deleted_body_stays(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    cal.write("o.ics", ics("uid-o", "Place", extra="LOCATION:Office\r\nDESCRIPTION:Description stays"))
    a = await first_sync(eas_bridge)
    sid = tags.text_from(elements(a)[0], "AirSync:ServerId")
    await eas_bridge.command(
        "Sync",
        sync_request(
            key(a), "test-calendar", commands=[k("AirSync:Change", k("AirSync:ServerId", text=sid), appointment_data("Place", start="20261201T090000Z", end="20261201T100000Z", reminder=None))]
        ),
    )
    text = cal.dav_objects["o.ics"][1]
    assert "LOCATION" not in text and "Description stays" in text


async def test_delta_without_times_deletes_nothing(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    cal.write("d.ics", ics("uid-d", "Delta", extra="LOCATION:Office"))
    a = await first_sync(eas_bridge)
    sid = tags.text_from(elements(a)[0], "AirSync:ServerId")
    await eas_bridge.command(
        "Sync", sync_request(key(a), "test-calendar", commands=[k("AirSync:Change", k("AirSync:ServerId", text=sid), k("AirSync:ApplicationData", k("Calendar:Subject", text="Delta new")))])
    )
    text = cal.dav_objects["d.ics"][1]
    assert "Delta new" in text and "LOCATION:Office" in text


async def test_supported_empty_means_everything_ghosted(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    cal.write("g.ics", ics("uid-gh", "Ghost", extra="LOCATION:Office"))
    request = sync_request("0", "test-calendar")
    coll = collection(request)
    with_supported = k("AirSync:Sync", k("AirSync:Collections", k("AirSync:Collection", *coll.children, k("AirSync:Supported"))))
    a = await eas_bridge.command("Sync", with_supported)
    a = await eas_bridge.command("Sync", sync_request(key(a), "test-calendar"))
    sid = tags.text_from(elements(a)[0], "AirSync:ServerId")
    await eas_bridge.command(
        "Sync",
        sync_request(
            key(a), "test-calendar", commands=[k("AirSync:Change", k("AirSync:ServerId", text=sid), appointment_data("Ghost", start="20261201T090000Z", end="20261201T100000Z", reminder=None))]
        ),
    )
    assert "LOCATION:Office" in cal.dav_objects["g.ics"][1]


async def test_protected_appointment_only_title_location_time(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    cal.write("h.ics", ics("uid-h", "External", extra="X-EXAMPLE-PROTECTED-CREATED-BY:external-writer"))
    a = await first_sync(eas_bridge)
    sid = tags.text_from(elements(a)[0], "AirSync:ServerId")
    a = await eas_bridge.command(
        "Sync",
        sync_request(
            key(a),
            "test-calendar",
            commands=[k("AirSync:Change", k("AirSync:ServerId", text=sid), appointment_data("External private", start="20261201T090000Z", end="20261201T100000Z", reminder=None, extra=[]))],
        ),
    )
    assert "External private" in cal.dav_objects["h.ics"][1]
    a = await eas_bridge.command(
        "Sync",
        sync_request(
            key(a),
            "test-calendar",
            commands=[k("AirSync:Change", k("AirSync:ServerId", text=sid), appointment_data("External private", start="20261201T090000Z", end="20261201T100000Z", reminder=30))],
        ),
    )
    assert status(tags.find(tags.find(collection(a), "AirSync:Responses"), "AirSync:Change"), "AirSync:Status") == 7
    assert "VALARM" not in cal.dav_objects["h.ics"][1]


async def test_delete_brake(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    for i in range(12):
        cal.write(f"l{i}.ics", ics(f"uid-l{i}"))
    a = await first_sync(eas_bridge)
    sids = [tags.text_from(x, "AirSync:ServerId") for x in elements(a)]
    a = await eas_bridge.command("Sync", sync_request(key(a), "test-calendar", commands=[k("AirSync:Delete", k("AirSync:ServerId", text=x)) for x in sids]))
    assert len(cal.dav_objects) == 12 - 5
    assert len(elements(a)) == 7


async def test_add_without_uid_is_idempotent_via_client_id(eas_bridge, clock):
    a = await eas_bridge.command("Sync", sync_request("0", "test-calendar"))
    a = await eas_bridge.command("Sync", sync_request(key(a), "test-calendar", commands=[k("AirSync:Add", k("AirSync:ClientId", text="feste-id"), appointment_data("M-Idempotent"))]))
    clock.before(3600)
    eas_bridge.d.store._eas_expiry_s = 10**9
    a = await eas_bridge.command("Sync", sync_request("0", "test-calendar"), device="newdevice")
    await eas_bridge.command("Sync", sync_request(key(a), "test-calendar", commands=[k("AirSync:Add", k("AirSync:ClientId", text="feste-id"), appointment_data("M-Idempotent"))]), device="newdevice")
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    assert sum("M-Idempotent" in i for _, i in cal.dav_objects.values()) == 1


async def test_partial_and_too_many_collections(eas_bridge):
    assert status(await eas_bridge.command("Sync", k("AirSync:Sync", k("AirSync:Partial"))), "AirSync:Status") == 13
    many = k("AirSync:Sync", k("AirSync:Collections", *[collection(sync_request("0", "test-calendar"))] * 31))
    a = await eas_bridge.command("Sync", many)
    assert status(a, "AirSync:Status") == 15 and tags.text_from(a, "AirSync:Limit") == "30"


async def test_device_limit_displaces_instead_of_403(eas_bridge):
    content = k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=0))
    for i in range(8):
        assert await eas_bridge.command("FolderSync", content, device=f"device{i}") is not None
    assert len(await eas_bridge.d.store.eas_devices(USER)) == 6


async def test_content_type_short_form_allowed(eas_bridge):
    content = wbxml.encode(k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=0)))
    r = await eas_bridge.raw("FolderSync", content, header={"Content-Type": "application/vnd.ms-sync"})
    assert r.status_code == 200


async def test_nextcloud_disturbance_in_ping_is_503(eas_bridge):
    from davbridge.caldav import CaldavError, CaldavKind

    await first_sync(eas_bridge)
    eas_bridge.fake.failure = CaldavError(CaldavKind.BUSY, 503)
    r = await eas_bridge.raw("Ping", wbxml.encode(ping(60, "test-calendar")))
    eas_bridge.fake.failure = None
    assert r.status_code == 503 and r.headers["retry-after"] == "30"


# ------------------------------------------------------------------ Review A/B (2026-11-04 Abend)


async def _conflict_prepare(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    cal.write("k.ics", ics("uid-k", "Original"))
    a = await first_sync(eas_bridge)
    sid = tags.text_from(elements(a)[0], "AirSync:ServerId")
    cal.write("k.ics", ics("uid-k", "External neu", extra="X-EXAMPLE-PROTECTED-CREATED-BY:external-writer"))
    return cal, a, sid


async def test_nextcloud_wins_also_on_repeated_change(eas_bridge):
    cal, a, sid = await _conflict_prepare(eas_bridge)
    wish = appointment_data("Old state vom iPhone", start="20261201T090000Z", end="20261201T100000Z", reminder=None)
    for _ in range(3):
        a = await eas_bridge.command("Sync", sync_request(key(a), "test-calendar", commands=[k("AirSync:Change", k("AirSync:ServerId", text=sid), wish)]))
        assert status(tags.find(tags.find(collection(a), "AirSync:Responses"), "AirSync:Change"), "AirSync:Status") == 7
        assert "External neu" in cal.dav_objects["k.ics"][1] and "Old state" not in cal.dav_objects["k.ics"][1]
    new = appointment_data("Real new change", start="20261201T090000Z", end="20261201T100000Z", reminder=None)
    await eas_bridge.command("Sync", sync_request(key(a), "test-calendar", commands=[k("AirSync:Change", k("AirSync:ServerId", text=sid), new)]))
    assert "Real new change" in cal.dav_objects["k.ics"][1]


async def test_conflict_delete_is_rejected_twice_then_it_applies(eas_bridge):
    cal, a, sid = await _conflict_prepare(eas_bridge)
    delete = [k("AirSync:Delete", k("AirSync:ServerId", text=sid))]
    for i in range(2):
        a = await eas_bridge.command("Sync", sync_request(key(a), "test-calendar", commands=delete))
        assert status(tags.find(tags.find(collection(a), "AirSync:Responses"), "AirSync:Delete"), "AirSync:Status") == 7
        assert "k.ics" in cal.dav_objects
    await eas_bridge.command("Sync", sync_request(key(a), "test-calendar", commands=delete))
    assert "k.ics" not in cal.dav_objects


async def test_no_op_change_confirms_with_new_key_instead_of_empty_body(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    cal.write("n.ics", ics("uid-n", "Same", start="20261201T090000Z", end="20261201T100000Z"))
    a = await first_sync(eas_bridge)
    sid = tags.text_from(elements(a)[0], "AirSync:ServerId")
    old = key(a)
    equal = appointment_data("Same", start="20261201T090000Z", end="20261201T100000Z", reminder=None)
    r = await eas_bridge.command("Sync", sync_request(old, "test-calendar", commands=[k("AirSync:Change", k("AirSync:ServerId", text=sid), equal)]))
    assert r is not None and key(r) != old


async def test_delete_unknown_server_id_status_8(eas_bridge):
    a = await eas_bridge.command("Sync", sync_request("0", "test-calendar"))
    a = await eas_bridge.command("Sync", sync_request(key(a), "test-calendar", commands=[k("AirSync:Delete", k("AirSync:ServerId", text="000000000000"))]))
    assert status(tags.find(tags.find(collection(a), "AirSync:Responses"), "AirSync:Delete"), "AirSync:Status") == 8


async def test_client_id_is_not_in_sqlite_retry_still_knows_it(eas_bridge):
    a = await eas_bridge.command("Sync", sync_request("0", "test-calendar"))
    request = sync_request(key(a), "test-calendar", commands=[k("AirSync:Add", k("AirSync:ClientId", text="GEHEIME-CLIENTID-42"), appointment_data("M-CID"))])
    first = await eas_bridge.command("Sync", request)
    second = await eas_bridge.command("Sync", request)
    assert second == first
    assert tags.text_from(tags.find(tags.find(collection(second), "AirSync:Responses"), "AirSync:Add"), "AirSync:ClientId") == "GEHEIME-CLIENTID-42"
    for file in eas_bridge.d.config.state_dir.glob("davbridge.sqlite*"):
        assert b"GEHEIME-CLIENTID-42" not in file.read_bytes()


def test_sync_with_wait_and_commands_counts_against_the_account_limit():
    from davbridge.eas.http import is_waiting

    with_ = k("AirSync:Sync", k("AirSync:Collections", k("AirSync:Collection", k("AirSync:Commands", k("AirSync:Delete")))), k("AirSync:Wait", text=5))
    without = k("AirSync:Sync", k("AirSync:Collections", k("AirSync:Collection")), k("AirSync:Wait", text=5))
    assert is_waiting("Sync", with_) is False and is_waiting("Sync", without) is True and is_waiting("Ping", None) is True
    assert is_waiting("Sync", k("AirSync:Sync", k("AirSync:Collections", k("AirSync:Collection")))) is False


async def test_ping_ignores_invisible_changes(eas_bridge):
    from tests.doubles.fake_nextcloud import vtodo_ics

    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    await first_sync(eas_bridge)
    cal.write("task.ics", vtodo_ics("uid-todo"))
    eas_bridge.d.notifier.last_caldav_ok = lambda: None
    task = asyncio.create_task(eas_bridge.command("Ping", ping(60, "test-calendar")))
    await asyncio.sleep(0.3)
    assert not task.done()
    eas_bridge.d.eas.waker.end()
    assert status(await asyncio.wait_for(task, 5), "Ping:Status") == 1


async def test_wbxml_empty_strings_in_mass_is_fast(eas_bridge):
    import time

    raw = b"\x03\x01\x6a\x00\x45" + b"\x03\x00" * 200_000
    t0 = time.monotonic()
    r = await eas_bridge.raw("Sync", raw)
    assert r.status_code == 400 and time.monotonic() - t0 < 2


def test_config_is_type_strict(tmp_path):
    import copy

    from tests.conftest import TEST_CONFIG

    from davbridge.config import ConfigError, from_dict

    for bad in ({"enabled": "false"}, {"enabled": 1}, {"enabled": True, "window_max": True}, {"enabled": True, "window_max": "100"}, {"enabled": True, "delete_brake_minimum": 1.5}):
        raw = copy.deepcopy(TEST_CONFIG)
        raw["users"][0]["eas_enabled"] = True
        raw["protocols"]["eas"] = {"acknowledge_mobile_cloud_processing": True, **bad}
        with pytest.raises(ConfigError):
            from_dict(raw)


async def test_deliberate_body_truncation_is_written(eas_bridge):
    cal = eas_bridge.fake.cal(USER, TEST_CAL_URI)
    cal.write("kurz.ics", ics("uid-kurz", "Short", extra="DESCRIPTION:Projekt Alpha - ausfuehrliche Details"))
    a = await first_sync(eas_bridge)
    sid = tags.text_from(elements(a)[0], "AirSync:ServerId")
    back = appointment_data(
        "Short", start="20261201T090000Z", end="20261201T100000Z", reminder=None, extra=[k("AirSyncBase:Body", k("AirSyncBase:Type", text=1), k("AirSyncBase:Data", text="Projekt Alpha"))]
    )
    await eas_bridge.command("Sync", sync_request(key(a), "test-calendar", commands=[k("AirSync:Change", k("AirSync:ServerId", text=sid), back)]))
    text = cal.dav_objects["kurz.ics"][1].replace("\r\n ", "")
    assert "DESCRIPTION:Projekt Alpha\r\n" in text and "ausfuehrliche" not in text


async def test_client_creates_mail_folder_and_sees_it_afterwards(eas_bridge):
    """Finding from the first real attempt (Outlook-iOS-Android/1.0): FolderCreate type 12 must succeed, otherwise the setup aborts."""
    a = await eas_bridge.command("FolderSync", k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=0)))
    sk = tags.text_from(a, "FolderHierarchy:SyncKey")

    def create(key, name="Test User", type=12, parents="0"):
        return k(
            "FolderHierarchy:FolderCreate",
            k("FolderHierarchy:SyncKey", text=key),
            k("FolderHierarchy:ParentId", text=parents),
            k("FolderHierarchy:DisplayName", text=name),
            k("FolderHierarchy:Type", text=type),
        )

    r = await eas_bridge.command("FolderCreate", create(sk))
    assert status(r, "FolderHierarchy:Status") == 1
    new_id, new_key = tags.text_from(r, "FolderHierarchy:ServerId"), tags.text_from(r, "FolderHierarchy:SyncKey")
    assert new_id.startswith("u-") and new_key != sk
    listing = await eas_bridge.command("FolderSync", k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=new_key)))
    assert tags.text_from(tags.find(listing, "FolderHierarchy:Changes"), "FolderHierarchy:Count") == "0"
    full = await eas_bridge.command("FolderSync", k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=0)))
    names = {tags.text_from(x, "FolderHierarchy:ServerId"): tags.text_from(x, "FolderHierarchy:DisplayName") for x in tags.find_all(tags.find(full, "FolderHierarchy:Changes"), "FolderHierarchy:Add")}
    assert names[new_id] == "Test User"
    assert tags.text_from(full, "FolderHierarchy:SyncKey") == new_key
    s0 = await eas_bridge.command("Sync", sync_request("0", new_id))
    assert status(collection(s0), "AirSync:Status") == 1
    assert status(await eas_bridge.command("FolderCreate", create(sk, "Another one")), "FolderHierarchy:Status") == 9
    assert status(await eas_bridge.command("FolderCreate", create(new_key, "Test User")), "FolderHierarchy:Status") == 2
    assert status(await eas_bridge.command("FolderCreate", create(new_key, "Calendar", type=13)), "FolderHierarchy:Status") == 3
    assert status(await eas_bridge.command("FolderCreate", create(new_key, "Unter", parents="inbox")), "FolderHierarchy:Status") == 5
    gone = await eas_bridge.command("FolderDelete", k("FolderHierarchy:FolderDelete", k("FolderHierarchy:SyncKey", text=new_key), k("FolderHierarchy:ServerId", text=new_id)))
    assert status(gone, "FolderHierarchy:Status") == 1


async def test_oof_disable_is_success_enable_is_not(eas_bridge):
    out = await eas_bridge.command("Settings", k("Settings:Settings", k("Settings:Oof", k("Settings:Set", k("Settings:OofState", text=0)))))
    assert status(out, "Settings:Status") == 1
    an = await eas_bridge.command("Settings", k("Settings:Settings", k("Settings:Oof", k("Settings:Set", k("Settings:OofState", text=1)))))
    assert status(an, "Settings:Status") == 7
