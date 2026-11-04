"""Real CalDAV server, only the account test-user and its test calendar. Appointments are synthetic."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from davbridge.caldav import CaldavError, CaldavKind

pytestmark = pytest.mark.integration


def ics(uid: str, title: str = "T-Synthetic") -> str:
    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//test//en",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            "DTSTAMP:20261101T120000Z",
            "DTSTART:20261201T090000Z",
            "DTEND:20261201T100000Z",
            f"SUMMARY:{title}",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )


async def test_access(caldav, credentials):
    assert await caldav.check_access(credentials) is True
    from davbridge.caldav import Credentials

    assert await caldav.check_access(Credentials(credentials.user, "wrong-wrong")) is False


async def test_calendar_info_and_permissions(caldav, credentials, test_calendar, read_only):
    a = await caldav.calendar_info(credentials, test_calendar)
    assert a.writable and a.sync_token and "VEVENT" in a.components
    n = await caldav.calendar_info(credentials, read_only)
    if n.writable:
        pytest.skip("the read-only calendar is writable on this server (it needs a share with read-only permission)")
    assert not n.writable


async def test_read_only_calendar_refuses_writing(caldav, credentials, read_only):
    if (await caldav.calendar_info(credentials, read_only)).writable:
        pytest.skip("the read-only calendar is writable on this server (it needs a share with read-only permission)")
    with pytest.raises(CaldavError) as e:
        await caldav.create(credentials, read_only, f"x{uuid.uuid4().hex}.ics", ics(uuid.uuid4().hex))
    assert e.value.kind == CaldavKind.FORBIDDEN


async def test_crud_etag_conflict_and_sync(caldav, credentials, test_calendar):
    uid = "t" + uuid.uuid4().hex
    href = uid + ".ics"
    start = await caldav.sync(credentials, test_calendar, None)
    assert start.full
    etag = await caldav.create(credentials, test_calendar, href, ics(uid))
    try:
        d = await caldav.sync(credentials, test_calendar, start.token)
        assert href in d.changed and not d.full
        obj = await caldav.get(credentials, test_calendar, href)
        assert obj and "T-Synthetic" in obj.ics
        if etag:
            assert etag == obj.etag
        with pytest.raises(CaldavError) as e:
            await caldav.create(credentials, test_calendar, href, ics(uid))
        assert e.value.kind == CaldavKind.CONFLICT
        new = await caldav.change(credentials, test_calendar, href, ics(uid, "T-Geaendert"), obj.etag)
        with pytest.raises(CaldavError) as e:
            await caldav.change(credentials, test_calendar, href, ics(uid, "T-Alt"), obj.etag)
        assert e.value.kind == CaldavKind.CONFLICT
        more = await caldav.get_several(credentials, test_calendar, [href, "does-not-exist.ics"])
        assert [m.href for m in more] == [href] and "T-Geaendert" in more[0].ics
        sz = await caldav.time_range(credentials, test_calendar, datetime(2026, 12, 1, tzinfo=timezone.utc), datetime(2026, 12, 2, tzinfo=timezone.utc))
        assert href in [o.href for o in sz]
        assert (await caldav.search_uid(credentials, test_calendar, uid)).href == href
        d2 = await caldav.sync(credentials, test_calendar, d.token)
        assert href in d2.changed
        with pytest.raises(CaldavError) as e:
            await caldav.delete(credentials, test_calendar, href, '"wrong"')
        assert e.value.kind == CaldavKind.CONFLICT
        print("PUT-ETag geliefert:", bool(etag), bool(new))
    finally:
        o = await caldav.get(credentials, test_calendar, href)
        if o:
            assert await caldav.delete(credentials, test_calendar, href, o.etag)
    d3 = await caldav.sync(credentials, test_calendar, d2.token)
    assert href in d3.deleted
    with pytest.raises(CaldavError) as e:
        await caldav.delete(credentials, test_calendar, href, '"egal"')
    assert e.value.kind == CaldavKind.CONFLICT
    assert await caldav.get(credentials, test_calendar, href) is None


async def test_invalid_sync_token(caldav, credentials, test_calendar):
    with pytest.raises(CaldavError) as e:
        await caldav.sync(credentials, test_calendar, "unsinn")
    assert e.value.kind == CaldavKind.TOKEN_INVALID


async def test_uid_conflict_in_other_file(caldav, credentials, test_calendar):
    uid = "t" + uuid.uuid4().hex
    await caldav.create(credentials, test_calendar, uid + ".ics", ics(uid))
    try:
        with pytest.raises(CaldavError) as e:
            await caldav.create(credentials, test_calendar, "andere-" + uid + ".ics", ics(uid))
        assert e.value.kind == CaldavKind.UID_CONFLICT
    finally:
        o = await caldav.get(credentials, test_calendar, uid + ".ics")
        await caldav.delete(credentials, test_calendar, uid + ".ics", o.etag)


async def test_autodiscovery_lists_own_calendars_and_hides_shares_by_default(caldav, credentials, test_calendar, read_only):
    own = await caldav.discover_calendars(credentials, include_shared=False)
    assert test_calendar.uri in {c.uri for c in own}
    assert all(not c.shared for c in own)
    everything = await caldav.discover_calendars(credentials, include_shared=True)
    assert {c.uri for c in own} <= {c.uri for c in everything}
    by_uri = {c.uri: c for c in everything}
    assert by_uri[test_calendar.uri].writable
    if read_only.uri in by_uri:
        assert by_uri[read_only.uri].shared or not by_uri[read_only.uri].writable
