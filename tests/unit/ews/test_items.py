from tests import soap_helpers as h
from tests.doubles.fake_nextcloud import ics, series_ics
from tests.unit.conftest import READ_ONLY, TEST_CAL, USER, codes, messages_xml, ops
from tests.unit.ics_helpers import golden

from davbridge.caldav import CaldavError, CaldavKind
from davbridge.identifiers import ElementRef

NS = h.NS
ZONE = "W. Europe Standard Time"
START, END = "2026-12-02T09:00:00Z", "2026-12-02T10:00:00Z"


def item(e, i=0):
    el = messages_xml(e)[i].find(".//t:ItemId", NS)
    return el.get("Id"), el.get("ChangeKey")


def ref(d, item_id):
    return d.ids.read_element_id(USER, item_id)


def dav_objects(fake, uri=TEST_CAL):
    return fake.cal(USER, uri).dav_objects


async def put(d, fake, title="Creation", extra="", **kw):
    e = await ops(d, h.create_item(title, START, END, "Room", extra=extra, **kw), zone=ZONE)
    assert codes(e) == ["NoError"], codes(e)
    return item(e)


# ------------------------------------------------------------------ CreateItem


async def test_create_without_uid_creates_one_object_and_is_idempotent(services, fake):
    a_id, a_ck = await put(services, fake)
    assert len(dav_objects(fake)) == 1
    text = next(iter(dav_objects(fake).values()))[1]
    assert "SUMMARY:Creation" in text and "X-DAVBRIDGE-CREATED-BY:outlook" in text and "ATTENDEE" not in text and "ORGANIZER" not in text
    b_id, b_ck = await put(services, fake)
    assert len(dav_objects(fake)) == 1 and b_id == a_id and b_ck == a_ck


async def test_create_without_uid_different_contents_are_different_appointments(services, fake):
    await put(services, fake, "One")
    await put(services, fake, "Two")
    assert len(dav_objects(fake)) == 2


async def test_create_with_uid_retry_yields_one_object(services, fake):
    a_id, _ = await put(services, fake, extra="<t:UID>uid-fest-1</t:UID>")
    b_id, _ = await put(services, fake, extra="<t:UID>uid-fest-1</t:UID>")
    assert len(dav_objects(fake)) == 1 and a_id == b_id
    assert ref(services, a_id).href == "uid-fest-1.ics"


async def test_create_uid_conflict_in_other_file_returns_existing(services, fake):
    fake.cal(USER, TEST_CAL).write("foreigner-name.ics", ics("uid-doppelt", "Existing"))
    i, _ = await put(services, fake, extra="<t:UID>uid-doppelt</t:UID>")
    assert ref(services, i).href == "foreigner-name.ics" and len(dav_objects(fake)) == 1


async def test_create_when_put_returns_no_etag(services, fake):
    fake.put_without_etag = True
    i, ck = await put(services, fake)
    assert services.ids.read_change_key(USER, ref(services, i), ck) == next(iter(dav_objects(fake).values()))[0]


async def test_create_in_read_only_calendar_is_refused(services, fake):
    fake.cal(USER, READ_ONLY, writable=False)
    target = services.ids.folder_id(USER, "read-only-calendar")
    xml = h.create_item("X", START, END).replace('<t:DistinguishedFolderId Id="calendar"/>', f'<t:FolderId Id="{target}"/>')
    assert codes(await ops(services, xml, zone=ZONE)) == ["ErrorAccessDenied"]
    assert not dav_objects(fake, READ_ONLY)


async def test_create_with_attendees_is_rejected(services, fake):
    extra = "<t:RequiredAttendees><t:Attendee><t:Mailbox><t:EmailAddress>a@b.test</t:EmailAddress></t:Mailbox></t:Attendee></t:RequiredAttendees>"
    assert codes(await ops(services, h.create_item("X", START, END, extra=extra), zone=ZONE)) == ["ErrorInvalidOperation"]
    assert not dav_objects(fake)


async def test_create_unknown_field_strictly_rejected_nothing_written(services, fake):
    assert codes(await ops(services, h.create_item("X", START, END, extra="<t:DoesNotExist>1</t:DoesNotExist>"), zone=ZONE)) == ["ErrorInvalidRequest"]
    assert not dav_objects(fake)


async def test_create_message_instead_of_appointment_is_access_denied(services, fake):
    xml = '<m:CreateItem MessageDisposition="SendAndSaveCopy"><m:Items><t:Message><t:Subject>Mail</t:Subject></t:Message></m:Items></m:CreateItem>'
    assert codes(await ops(services, xml)) == ["ErrorAccessDenied"]


async def test_create_end_before_start_and_unknown_zone(services, fake):
    assert codes(await ops(services, h.create_item("X", END, START), zone=ZONE)) == ["ErrorCalendarEndDateIsEarlierThanStartDate"]
    assert codes(await ops(services, h.create_item("X", START, END), zone="Exists Not Standard Time")) == ["ErrorTimeZone"]
    assert not dav_objects(fake)


async def test_create_without_items_is_error(services):
    assert codes(await ops(services, "<m:CreateItem><m:Items/></m:CreateItem>")) == ["ErrorInvalidRequest"]


async def test_create_nextcloud_outage(services, fake):
    fake.failure = CaldavError(CaldavKind.BUSY, 503)
    assert codes(await ops(services, h.create_item("X", START, END), zone=ZONE)) == ["ErrorServerBusy"]


# ------------------------------------------------------------------ UpdateItem


async def test_update_patches_and_preserves_unknown_properties(services, fake):
    fake.cal(USER, TEST_CAL).write("h.ics", golden("custom-properties-single.ics"))
    full = await ops(services, h.get_item(services.ids.element_id(USER, __import__("davbridge.identifiers", fromlist=["ElementRef"]).ElementRef("test-calendar", "h.ics"))))
    i, ck = item(full)
    e = await ops(services, h.update_subject(i, ck, "New title"))
    assert codes(e) == ["NoError"]
    _, new_ck = item(e)
    assert new_ck != ck
    text = dav_objects(fake)["h.ics"][1]
    assert "SUMMARY:New title" in text and "X-EXAMPLE-PROTECTED-CREATED-BY:external-writer" in text and "X-EXAMPLE-PROTECTED-KEY:wa-key-4711" in text and "BEGIN:VALARM" in text
    assert services.ids.read_change_key(USER, ref(services, i), new_ck) == dav_objects(fake)["h.ics"][0]


async def test_update_with_outdated_changekey_is_conflict_and_changes_nothing(services, fake):
    i, ck = await put(services, fake)
    assert codes(await ops(services, h.update_subject(i, ck, "First change"))) == ["NoError"]
    before = dict(dav_objects(fake))
    assert codes(await ops(services, h.update_subject(i, ck, "Second, stale"))) == ["ErrorIrresolvableConflict"]
    assert dav_objects(fake) == before


async def test_update_retry_with_old_changekey_is_success_without_writing(services, fake):
    i, ck = await put(services, fake)
    assert codes(await ops(services, h.update_subject(i, ck, "Same"))) == ["NoError"]
    etag = next(iter(dav_objects(fake).values()))[0]
    assert codes(await ops(services, h.update_subject(i, ck, "Same"))) == ["NoError"]
    assert next(iter(dav_objects(fake).values()))[0] == etag


async def test_update_changekey_missing_or_invalid(services, fake):
    i, ck = await put(services, fake)
    j, ck_j = await put(services, fake, "Second")
    assert codes(await ops(services, h.update_subject(i, "broken", "X"))) == ["ErrorInvalidChangeKey"]
    assert codes(await ops(services, h.update_subject(i, ck_j, "X"))) == ["ErrorInvalidChangeKey"]


async def test_update_foreign_or_broken_item_id(services, fake):
    assert codes(await ops(services, h.update_subject("broken", "x", "X"))) == ["ErrorInvalidIdMalformed"]
    foreign = services.ids.element_id("other", __import__("davbridge.identifiers", fromlist=["ElementRef"]).ElementRef("test-calendar", "a.ics"))
    assert codes(await ops(services, h.update_subject(foreign, "x", "X"))) == ["ErrorInvalidIdMalformed"]


async def test_update_missing_element(services, fake):
    i, ck = await put(services, fake)
    fake.cal(USER, TEST_CAL).remove(ref(services, i).href)
    assert codes(await ops(services, h.update_subject(i, ck, "X"))) == ["ErrorItemNotFound"]


async def test_update_in_read_only_calendar_and_with_attendees(services, fake):
    from davbridge.identifiers import ElementRef

    fake.cal(USER, READ_ONLY, writable=False).write("r.ics", ics("uid-r", "Read only"))
    read = await ops(services, h.get_item(services.ids.element_id(USER, ElementRef("read-only-calendar", "r.ics"))))
    i, ck = item(read)
    assert codes(await ops(services, h.update_subject(i, ck, "X"))) == ["ErrorAccessDenied"]
    fake.cal(USER, TEST_CAL).write("m.ics", golden("with-attendees.ics"))
    read_back = await ops(services, h.get_item(services.ids.element_id(USER, ElementRef("test-calendar", "m.ics"))))
    i2, ck2 = item(read_back)
    assert codes(await ops(services, h.update_subject(i2, ck2, "X"))) == ["ErrorAccessDenied"]


async def test_update_unknown_field_all_or_nothing(services, fake):
    i, ck = await put(services, fake, "Original")
    xml = (
        f'<m:UpdateItem ConflictResolution="AutoResolve"><m:ItemChanges><t:ItemChange><t:ItemId Id="{i}" ChangeKey="{ck}"/><t:Updates>'
        '<t:SetItemField><t:FieldURI FieldURI="item:Subject"/><t:CalendarItem><t:Subject>Sollte not ankommen</t:Subject></t:CalendarItem></t:SetItemField>'
        '<t:SetItemField><t:FieldURI FieldURI="item:DoesNotExist"/><t:CalendarItem><t:DoesNotExist>1</t:DoesNotExist></t:CalendarItem></t:SetItemField>'
        "</t:Updates></t:ItemChange></m:ItemChanges></m:UpdateItem>"
    )
    before = dict(dav_objects(fake))
    assert codes(await ops(services, xml)) == ["ErrorUnsupportedPathForSet"]
    assert dav_objects(fake) == before


async def test_update_attendee_field_is_rejected(services, fake):
    i, ck = await put(services, fake)
    xml = (
        f'<m:UpdateItem><m:ItemChanges><t:ItemChange><t:ItemId Id="{i}" ChangeKey="{ck}"/><t:Updates><t:SetItemField>'
        '<t:FieldURI FieldURI="calendar:RequiredAttendees"/><t:CalendarItem><t:RequiredAttendees><t:Attendee><t:Mailbox><t:EmailAddress>a@b.test</t:EmailAddress></t:Mailbox></t:Attendee></t:RequiredAttendees></t:CalendarItem>'
        "</t:SetItemField></t:Updates></t:ItemChange></m:ItemChanges></m:UpdateItem>"
    )
    assert codes(await ops(services, xml)) == ["ErrorInvalidOperation"]


async def test_update_without_changekey_applies(services, fake):
    d = services
    i, _ = await put(d, fake, "Original")
    assert codes(await ops(d, h.update_subject(i, None, "Without key"))) == ["NoError"]
    assert "SUMMARY:Without key" in next(iter(dav_objects(fake).values()))[1]


async def test_update_empty_updates_write_nothing(services, fake):
    d = services
    i, ck = await put(d, fake, "Original")
    before = dict(dav_objects(fake))
    xml = f'<m:UpdateItem><m:ItemChanges><t:ItemChange><t:ItemId Id="{i}"/><t:Updates/></t:ItemChange></m:ItemChanges></m:UpdateItem>'
    assert codes(await ops(d, xml)) == ["NoError"]
    assert dav_objects(fake) == before


async def test_update_412_despite_matching_changekey_is_conflict(services, fake):
    i, ck = await put(services, fake)

    async def race(*a, **k):
        raise CaldavError(CaldavKind.CONFLICT, 412)

    fake.change = race
    assert codes(await ops(services, h.update_subject(i, ck, "Race"))) == ["ErrorIrresolvableConflict"]


async def test_update_conflict_resolution_always_overwrite_does_not_overwrite(services, fake):
    i, ck = await put(services, fake)
    fake.cal(USER, TEST_CAL).write(ref(services, i).href, ics("uid-x", "Other client"))
    xml = h.update_subject(i, ck, "Outlook").replace('ConflictResolution="AutoResolve"', 'ConflictResolution="AlwaysOverwrite"')
    assert codes(await ops(services, xml)) == ["ErrorIrresolvableConflict"]
    assert "Other client" in next(iter(dav_objects(fake).values()))[1]


async def test_update_nextcloud_outage(services, fake):
    i, ck = await put(services, fake)
    fake.failure = CaldavError(CaldavKind.BUSY, 503)
    assert codes(await ops(services, h.update_subject(i, ck, "X"))) == ["ErrorServerBusy"]


# ------------------------------------------------------------------ DeleteItem


async def test_delete_with_changekey(services, fake):
    i, ck = await put(services, fake)
    assert codes(await ops(services, h.delete_item(i, ck))) == ["NoError"]
    assert not dav_objects(fake)


async def test_without_changekey_the_last_seen_state_applies(services, fake):
    i, _ = await put(services, fake, "Original")
    assert codes(await ops(services, h.update_subject(i, None, "From Outlook"))) == ["NoError"]
    href = next(iter(dav_objects(fake)))
    text = dav_objects(fake)[href][1]
    fake.cal(USER, TEST_CAL).write(href, text.replace("SUMMARY:From Outlook", "SUMMARY:From the server"))
    before = dict(dav_objects(fake))
    assert codes(await ops(services, h.update_subject(i, None, "Outdated aus Outlook"))) == ["ErrorIrresolvableConflict"]
    assert codes(await ops(services, h.delete_item(i, None))) == ["ErrorIrresolvableConflict"]
    assert dav_objects(fake) == before
    assert codes(await ops(services, h.get_item(i))) == ["NoError"]
    assert codes(await ops(services, h.update_subject(i, None, "Nach Abgleich"))) == ["NoError"]
    assert "SUMMARY:Nach Abgleich" in next(iter(dav_objects(fake).values()))[1]
    assert codes(await ops(services, h.delete_item(i, None))) == ["NoError"] and not dav_objects(fake)


async def test_without_changekey_and_without_anchor_the_current_state_applies(services, fake):
    fake.cal(USER, TEST_CAL).write("x.ics", ics("uid-x", "Foreign"))
    i = services.ids.element_id(USER, ElementRef("test-calendar", "x.ics"))
    assert codes(await ops(services, h.update_subject(i, None, "New"))) == ["NoError"]
    assert "SUMMARY:New" in dav_objects(fake)["x.ics"][1]


async def test_delete_without_changekey_uses_the_known_anchor(services, fake):
    i, _ = await put(services, fake)
    assert codes(await ops(services, h.delete_item(i, None))) == ["NoError"] and not dav_objects(fake)


async def test_delete_missing_object_is_success(services, fake):
    i, ck = await put(services, fake)
    fake.cal(USER, TEST_CAL).remove(ref(services, i).href)
    assert codes(await ops(services, h.delete_item(i, ck))) == ["NoError"]


async def test_delete_race_412_and_object_meanwhile_gone_is_success(services, fake):
    i, ck = await put(services, fake)
    k = fake.cal(USER, TEST_CAL)
    original = fake.delete

    async def race(z, cal, href, if_match):
        k.remove(href)
        return await original(z, cal, href, if_match)

    fake.delete = race
    assert codes(await ops(services, h.delete_item(i, ck))) == ["NoError"]


async def test_delete_with_outdated_changekey_is_conflict(services, fake):
    i, ck = await put(services, fake)
    fake.cal(USER, TEST_CAL).write(ref(services, i).href, ics("uid-x", "Changed"))
    assert codes(await ops(services, h.delete_item(i, ck))) == ["ErrorIrresolvableConflict"]
    assert len(dav_objects(fake)) == 1


async def test_delete_in_read_only_calendar_and_with_attendees(services, fake):
    from davbridge.identifiers import ElementRef

    fake.cal(USER, READ_ONLY, writable=False).write("r.ics", ics("uid-r", "Read only"))
    i, ck = item(await ops(services, h.get_item(services.ids.element_id(USER, ElementRef("read-only-calendar", "r.ics")))))
    assert codes(await ops(services, h.delete_item(i, ck))) == ["ErrorAccessDenied"]
    fake.cal(USER, TEST_CAL).write("m.ics", golden("with-attendees.ics"))
    i2, ck2 = item(await ops(services, h.get_item(services.ids.element_id(USER, ElementRef("test-calendar", "m.ics")))))
    assert codes(await ops(services, h.delete_item(i2, ck2))) == ["ErrorAccessDenied"]
    assert "m.ics" in dav_objects(fake)


# ------------------------------------------------------------------ GetItem / FindItem


async def test_get_item_mixed_in_request_order(services, fake):
    from davbridge.identifiers import ElementRef

    k = fake.cal(USER, TEST_CAL)
    k.write("a.ics", ics("uid-a", "A"))
    k.write("s.ics", series_ics("uid-s"))
    k.hidden.add("p.ics")
    ids = services.ids
    listing = [ids.element_id(USER, ElementRef("test-calendar", n)) for n in ("a.ics", "fehlt.ics", "s.ics", "p.ics")]
    e = await ops(services, h.get_item(listing[0], "broken", *listing[1:]))
    assert codes(e) == ["NoError", "ErrorInvalidIdMalformed", "ErrorItemNotFound", "ErrorItemNotFound", "ErrorItemNotFound"]
    assert messages_xml(e)[0].find(".//t:Subject", NS).text == "A"


async def test_get_item_groups_one_multiget_per_calendar(services, fake):
    from davbridge.identifiers import ElementRef

    fake.cal(USER, TEST_CAL).write("a.ics", ics("uid-a", "A"))
    fake.cal(USER, TEST_CAL).write("b.ics", ics("uid-b", "B"))
    fake.cal(USER, READ_ONLY, writable=False).write("c.ics", ics("uid-c", "C"))
    ids = [services.ids.element_id(USER, ElementRef(k, n)) for k, n in (("test-calendar", "a.ics"), ("read-only-calendar", "c.ics"), ("test-calendar", "b.ics"))]
    e = await ops(services, h.get_item(*ids))
    assert codes(e) == ["NoError"] * 3 and fake.multiget_calls == 2
    assert [messages_xml(e)[i].find(".//t:Subject", NS).text for i in range(3)] == ["A", "C", "B"]


def find_item(folder: str = "calendar", view: str = "", shape: str = "AllProperties") -> str:
    return (
        f'<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>{shape}</t:BaseShape></m:ItemShape>{view}'
        f'<m:ParentFolderIds><t:DistinguishedFolderId Id="{folder}"/></m:ParentFolderIds></m:FindItem>'
    )


async def test_find_item_list_sorted_without_unrepresentable(services, fake):
    k = fake.cal(USER, TEST_CAL)
    for n in ("b.ics", "a.ics"):
        k.write(n, ics("uid-" + n, n))
    k.write("s.ics", series_ics("uid-s"))
    e = await ops(services, find_item())
    assert codes(e) == ["NoError"]
    assert [x.text for x in messages_xml(e)[0].iter("{%s}Subject" % h.T)] == ["a.ics", "b.ics"]
    assert messages_xml(e)[0].find(".//m:RootFolder", NS).get("TotalItemsInView") == "2"


async def test_find_item_calendar_view_and_limit(services, fake):
    k = fake.cal(USER, TEST_CAL)
    for i in range(4):
        k.write(f"e{i}.ics", ics(f"uid-{i}", f"E{i}"))
    view = '<m:CalendarView MaxEntriesReturned="2" StartDate="2026-12-01T00:00:00Z" EndDate="2026-12-31T00:00:00Z"/>'
    e = await ops(services, find_item(view=view))
    root = messages_xml(e)[0].find(".//m:RootFolder", NS)
    assert root.get("TotalItemsInView") == "4" and root.get("IncludesLastItemInRange") == "false"
    assert len(root.findall(".//t:CalendarItem", NS)) == 2


async def test_find_item_mail_folder_empty_and_unknown(services):
    e = await ops(services, find_item("inbox"))
    assert codes(e) == ["NoError"] and messages_xml(e)[0].find(".//m:RootFolder", NS).get("TotalItemsInView") == "0"
    assert codes(await ops(services, find_item("archivemsgfolderroot"))) == ["ErrorFolderNotFound"]


async def test_find_item_idonly_with_additional_fields_returns_content(services, fake):
    fake.cal(USER, TEST_CAL).write("a.ics", ics("uid-a", "Extra"))
    xml = (
        '<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape><t:AdditionalProperties>'
        '<t:FieldURI FieldURI="calendar:Start"/><t:FieldURI FieldURI="calendar:End"/></t:AdditionalProperties></m:ItemShape>'
        '<m:CalendarView StartDate="2026-11-29T00:00:00Z" EndDate="2027-01-07T00:00:00Z"/>'
        '<m:ParentFolderIds><t:DistinguishedFolderId Id="calendar"/></m:ParentFolderIds></m:FindItem>'
    )
    el = messages_xml(await ops(services, xml))[0]
    assert el.find(".//t:CalendarItem/t:Start", NS) is not None and el.find(".//t:CalendarItem/t:End", NS) is not None


async def test_find_item_idonly_has_changekey_without_content(services, fake):
    fake.cal(USER, TEST_CAL).write("a.ics", ics("uid-a", "A"))
    el = messages_xml(await ops(services, find_item(shape="IdOnly")))[0]
    assert el.find(".//t:ItemId", NS).get("ChangeKey") and el.find(".//t:Subject", NS) is None


# ------------------------------------------------------------------ HTTP level (end to end with FakeNextcloud)


async def test_http_create_sync_update_delete(bridge, fake):
    from tests.unit.conftest import post, text

    client, d = bridge
    cal = d.ids.folder_id(USER, "test-calendar")
    s0 = text(await post(client, h.sync_items(cal)), "SyncState")
    w = await post(client, h.create_item("HTTP", START, END), zone=ZONE)
    i, ck = w.find(".//{%s}ItemId" % h.T).get("Id"), w.find(".//{%s}ItemId" % h.T).get("ChangeKey")
    w = await post(client, h.sync_items(cal, s0))
    assert w.find(".//{%s}Create" % h.T) is not None
    w = await post(client, h.update_subject(i, ck, "HTTP neu"))
    ck2 = w.find(".//{%s}ItemId" % h.T).get("ChangeKey")
    w = await post(client, h.delete_item(i, ck2))
    assert h.codes(w) == ["NoError"] and not dav_objects(fake)


# ------------------------------------------------------------------ Review-Befunde 2026-11-01


async def test_find_item_indexed_page_pages_with_offset(services, fake):
    k = fake.cal(USER, TEST_CAL)
    for i in range(4):
        k.write(f"e{i}.ics", ics(f"uid-{i}", f"E{i}"))

    def page(offset: int) -> str:
        return f'<m:IndexedPageItemView MaxEntriesReturned="2" Offset="{offset}" BasePoint="Beginning"/>'

    first = messages_xml(await ops(services, find_item(view=page(0))))[0].find(".//m:RootFolder", NS)
    second = messages_xml(await ops(services, find_item(view=page(2))))[0].find(".//m:RootFolder", NS)
    assert [x.text for x in first.iter("{%s}Subject" % h.T)] == ["E0", "E1"]
    assert first.get("IncludesLastItemInRange") == "false" and first.get("IndexedPagingOffset") == "2"
    assert [x.text for x in second.iter("{%s}Subject" % h.T)] == ["E2", "E3"]
    assert second.get("IncludesLastItemInRange") == "true" and second.get("IndexedPagingOffset") == "4"


async def test_find_item_calendar_view_sorted_by_start(services, fake):
    k = fake.cal(USER, TEST_CAL)
    k.write("a.ics", ics("uid-a", "Spaet", start="20261203T090000Z", end="20261203T100000Z"))
    k.write("z.ics", ics("uid-z", "Frueh", start="20261201T090000Z", end="20261201T100000Z"))
    view = '<m:CalendarView MaxEntriesReturned="1" StartDate="2026-12-01T00:00:00Z" EndDate="2026-12-31T00:00:00Z"/>'
    root = messages_xml(await ops(services, find_item(view=view)))[0].find(".//m:RootFolder", NS)
    assert [x.text for x in root.iter("{%s}Subject" % h.T)] == ["Frueh"]


async def test_create_without_uid_retry_after_lost_response_yields_one_object(services, fake):
    original = fake.create

    async def response_lost(z, cal, href, text):
        await original(z, cal, href, text)
        raise CaldavError(CaldavKind.BUSY, None)

    fake.create = response_lost
    e = await ops(services, h.create_item("Lost", START, END, "Room"), zone=ZONE)
    assert codes(e) == ["ErrorServerBusy"] and len(dav_objects(fake)) == 1
    fake.create = original
    e = await ops(services, h.create_item("Lost", START, END, "Room"), zone=ZONE)
    assert codes(e) == ["NoError"] and len(dav_objects(fake)) == 1


async def test_create_without_uid_retry_after_failed_put_writes_under_same_href(services, fake):
    original = fake.create

    async def fails(z, cal, href, text):
        raise CaldavError(CaldavKind.BUSY, None)

    fake.create = fails
    assert codes(await ops(services, h.create_item("New", START, END), zone=ZONE)) == ["ErrorServerBusy"]
    fake.create = original
    assert codes(await ops(services, h.create_item("New", START, END), zone=ZONE)) == ["NoError"]
    assert len(dav_objects(fake)) == 1


async def test_control_chars_in_the_appointment_do_not_stall_the_multi_response(services, fake):
    k = fake.cal(USER, TEST_CAL)
    k.write("a.ics", ics("uid-a", "Test\x0bTitle", extra="LOCATION:Room\x01 1"))
    k.write("b.ics", ics("uid-b", "Normal"))
    e = await ops(services, find_item())
    assert codes(e) == ["NoError"]
    assert sorted(x.text for x in messages_xml(e)[0].iter("{%s}Subject" % h.T)) == ["Normal", "TestTitle"]


async def test_oversized_ics_is_not_represented(services, fake):
    k = fake.cal(USER, TEST_CAL)
    k.write("large.ics", ics("uid-g", "Large", extra="DESCRIPTION:" + "x" * 300_000))
    k.write("small.ics", ics("uid-k", "Small"))
    e = await ops(services, find_item())
    assert [x.text for x in messages_xml(e)[0].iter("{%s}Subject" % h.T)] == ["Small"]
