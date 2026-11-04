import pytest
from tests import soap_helpers as h
from tests.doubles.fake_nextcloud import ics, series_ics, vtodo_ics
from tests.unit.conftest import READ_ONLY, TEST_CAL, USER, codes, messages_xml, ops

NS = h.NS


def folder(d, key="test-calendar") -> str:
    return d.ids.folder_id(USER, key)


async def sync(d, state=None, shape="IdOnly", max_n=512, key="test-calendar"):
    e = await ops(d, h.sync_items(folder(d, key), state, shape, max_n))
    return e


def changes(d, e, key="test-calendar") -> list[tuple[str, str]]:
    n = messages_xml(e)[0]
    result = []
    for wrapper in n.find("m:Changes", NS):
        kind = wrapper.tag.split("}")[1]
        item = wrapper.find(".//t:ItemId", NS)
        result.append((kind, d.ids.read_element_id(USER, item.get("Id")).href))
    return sorted(result)


def state_from(e) -> str:
    return messages_xml(e)[0].find("m:SyncState", NS).text


def last(e) -> str:
    return messages_xml(e)[0].find("m:IncludesLastItemInRange", NS).text


def populate(fake, n=3, uri=TEST_CAL):
    k = fake.cal(USER, uri, writable=(uri == TEST_CAL))
    for i in range(n):
        k.write(f"t{i}.ics", ics(f"uid-{i}", f"Appointment {i}"))
    return k


async def test_initial_sync_yields_creates_and_stable_state(services, fake):
    populate(fake)
    e = await sync(services)
    assert codes(e) == ["NoError"]
    assert changes(services, e) == [("Create", "t0.ics"), ("Create", "t1.ics"), ("Create", "t2.ics")]
    assert last(e) == "true"
    assert fake.sync_calls == [None]


async def test_no_change_gives_same_state_and_creates_nothing(services, fake):
    populate(fake)
    s1 = state_from(await sync(services))
    count = await services.store.count_states()
    for _ in range(3):
        e = await sync(services, s1)
        assert changes(services, e) == [] and state_from(e) == s1
    assert await services.store.count_states() == count


async def test_create_update_delete_in_sequence(services, fake):
    k = populate(fake)
    s1 = state_from(await sync(services))
    k.write("neu.ics", ics("uid-neu", "New"))
    k.write("t1.ics", ics("uid-1", "Changed"))
    k.remove("t2.ics")
    e = await sync(services, s1)
    assert changes(services, e) == [("Create", "neu.ics"), ("Delete", "t2.ics"), ("Update", "t1.ics")]
    assert [x for x in changes(services, await sync(services, state_from(e)))] == []


async def test_paging_over_max_changes_returned(services, fake):
    populate(fake, 5)
    e1 = await sync(services, max_n=2)
    assert len(changes(services, e1)) == 2 and last(e1) == "false"
    e2 = await sync(services, state_from(e1), max_n=2)
    assert len(changes(services, e2)) == 2 and last(e2) == "false"
    e3 = await sync(services, state_from(e2), max_n=2)
    assert len(changes(services, e3)) == 1 and last(e3) == "true"
    all = changes(services, e1) + changes(services, e2) + changes(services, e3)
    assert sorted(h for _, h in all) == [f"t{i}.ics" for i in range(5)] and {a for a, _ in all} == {"Create"}


async def test_old_state_repeated_gives_equal_changes(services, fake):
    k = populate(fake)
    s0 = state_from(await sync(services))
    k.write("neu.ics", ics("uid-neu", "New"))
    first = await sync(services, s0)
    second = await sync(services, s0)
    assert changes(services, first) == changes(services, second) == [("Create", "neu.ics")]
    assert state_from(second) != s0


async def test_token_invalid_leads_to_set_comparison(services, fake):
    k = populate(fake)
    s1 = state_from(await sync(services))
    k.write("neu.ics", ics("uid-neu", "New"))
    k.write("t1.ics", ics("uid-1", "Changed"))
    k.remove("t2.ics")
    k.min_token = k.n
    e = await sync(services, s1)
    assert changes(services, e) == [("Create", "neu.ics"), ("Delete", "t2.ics"), ("Update", "t1.ics")]
    assert fake.sync_calls[-1] is None


async def test_unchanged_calendar_after_token_truncation_stays_quiet(services, fake):
    k = populate(fake)
    s1 = state_from(await sync(services))
    k.min_token = k.n + 1
    k.n += 1
    e = await sync(services, s1)
    assert changes(services, e) == []


@pytest.mark.parametrize("variant", ["broken", "other_folder", "foreign_account", "discarded", "expired"])
async def test_invalid_state(services, fake, clock, variant):
    populate(fake)
    populate(fake, 1, READ_ONLY)
    s1 = state_from(await sync(services))
    if variant == "broken":
        state = s1[:-3] + ("AAA" if s1[-3:] != "AAA" else "BBB")
        key = "test-calendar"
    elif variant == "other_folder":
        state, key = s1, "read-only-calendar"
    elif variant == "foreign_account":
        zid = services.ids.read_sync_state(USER, "test-calendar", s1)
        state, key = services.ids.sync_state("other", "test-calendar", zid), "test-calendar"
    elif variant == "discarded":
        await services.store.discard_states(USER)
        state, key = s1, "test-calendar"
    else:
        clock.before(8 * 86400)
        await services.store.clean_up()
        state, key = s1, "test-calendar"
    e = await sync(services, state, key=key)
    assert codes(e) == ["ErrorInvalidSyncStateData"]


async def test_appointment_becomes_series_becomes_delete_and_comes_back(services, fake):
    k = populate(fake)
    s1 = state_from(await sync(services))
    k.write("t1.ics", series_ics("uid-1"))
    e = await sync(services, s1)
    assert changes(services, e) == [("Delete", "t1.ics")]
    s2 = state_from(e)
    assert changes(services, await sync(services, s2)) == []
    k.write("t1.ics", ics("uid-1", "Single again"))
    assert changes(services, await sync(services, s2)) == [("Create", "t1.ics")]


async def test_unrepresentable_is_never_delivered(services, fake):
    k = populate(fake, 1)
    k.write("series.ics", series_ics("uid-s"))
    k.write("todo.ics", vtodo_ics("uid-todo"))
    assert changes(services, await sync(services)) == [("Create", "t0.ics")]


async def test_object_in_sync_but_not_in_multiget_is_never_create(services, fake):
    k = populate(fake, 1)
    k.hidden.add("private.ics")
    k.n += 1
    k.changes.append((k.n, "private.ics"))
    e = await sync(services)
    assert changes(services, e) == [("Create", "t0.ics")]
    k.n += 1
    k.changes.append((k.n, "private.ics"))
    assert changes(services, await sync(services, state_from(e))) == []


async def test_previously_visible_object_becoming_invisible_gives_delete(services, fake):
    k = populate(fake, 2)
    s1 = state_from(await sync(services))
    k.write("t0.ics", ics("uid-0", "Now private"))
    k.hidden.add("t0.ics")
    assert changes(services, await sync(services, s1)) == [("Delete", "t0.ics")]


async def test_shapes_idonly_and_all_properties(services, fake):
    populate(fake, 1)
    short = messages_xml(await sync(services, shape="IdOnly"))[0]
    assert short.find(".//t:CalendarItem/t:ItemId", NS).get("ChangeKey") and short.find(".//t:Subject", NS) is None
    full = messages_xml(await sync(services, shape="AllProperties"))[0]
    assert full.find(".//t:Subject", NS).text == "Appointment 0"
    assert full.find(".//t:EffectiveRights/t:Modify", NS).text == "true"


async def test_read_only_calendar_yields_readable_items(services, fake):
    populate(fake, 1, READ_ONLY)
    full = messages_xml(await sync(services, shape="AllProperties", key="read-only-calendar"))[0]
    assert full.find(".//t:EffectiveRights/t:Modify", NS).text == "false"
    assert full.find(".//t:EffectiveRights/t:Read", NS).text == "true"
    assert full.find(".//t:IsOrganizer", NS).text == "false"


async def test_mail_folders_are_empty_with_fixed_state(services):
    e = await ops(services, h.sync_items(services.ids.folder_id(USER, "inbox")))
    assert changes(services, e) == [] and last(e) == "true"
    assert state_from(await ops(services, h.sync_items(services.ids.folder_id(USER, "inbox"), state_from(e)))) == state_from(e)


async def test_without_folder_error(services):
    e = await ops(services, "<m:SyncFolderItems><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape></m:ItemShape></m:SyncFolderItems>")
    assert codes(e) == ["ErrorFolderNotFound"]


async def test_nextcloud_outage_becomes_server_busy_with_backoff(services, fake):
    from davbridge.caldav import CaldavError, CaldavKind

    populate(fake)
    fake.failure = CaldavError(CaldavKind.BUSY, 503)
    e = await sync(services)
    assert codes(e) == ["ErrorServerBusy"]
    assert messages_xml(e)[0].find(".//t:Value[@Name='BackOffMilliseconds']", NS) is not None


async def test_delivery_is_logged_for_the_latency_measurement(services, fake):
    populate(fake, 2)
    await sync(services)
    lines = await services.store.events(kind="delivered")
    assert len(lines) == 2 and all(z[4] and len(z[4]) == 12 for z in lines)


@pytest.mark.parametrize("max_n", [0, -1])
async def test_max_changes_returned_out_of_range_is_rejected(services, fake, max_n):
    populate(fake, 2)
    assert codes(await sync(services, max_n=max_n)) == ["ErrorInvalidRequest"]
