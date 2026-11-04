"""Appointment <-> AirSync ApplicationData: golden trees, the reverse direction via ical.create/patch, permissions. All data is synthetic.

The golden trees live as text under tests/fixtures/eas/; with `UPDATE_GOLDEN=1` they are rewritten (then check them by hand).
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
from ics_helpers import GOLDEN_EAS, NOW, alarms, golden, properties, utc, vevent

from davbridge import ical
from davbridge.eas import calendar, recurrence, tags, timezone, wbxml
from davbridge.eas.calendar import Rejected
from davbridge.eas.wbxml import Node
from davbridge.permissions import NONE, Permissions

UTC = dt.timezone.utc
ACCOUNT = {"account_name": "Test User", "account_address": "test-user@dav.example.com"}
BERLIN = timezone.blob_base64("Europe/Berlin", utc(2026, 11, 1))
ONLY_METADATA = frozenset({"DTSTAMP", "LAST-MODIFIED", "SEQUENCE"})


def tree_text(k: Node, indentation: int = 0) -> str:
    line = " " * indentation + tags.display(k) + (f" = {k.text!r}" if k.text is not None else "")
    return "\n".join([line, *(tree_text(c, indentation + 2) for c in k.children)])


def output(ics: str, *, body_types=(1,), trunc=None, body_max=32 * 1024, appointment: ical.Appointment | None = None) -> Node | None:
    t = appointment or ical.read(ics)[0]
    return calendar.appointment_to_application_data(t, ics, body_types=body_types, trunc=trunc, body_max=body_max, **ACCOUNT)


def wire(k: Node) -> Node:
    """Once over the wire: encode and decode again."""
    return wbxml.decode(wbxml.encode(k))


def value(k: Node, name: str) -> str | None:
    return tags.text_from(k, name)


def check_golden(name: str, k: Node | None) -> None:
    text = "None" if k is None else tree_text(k)
    file = GOLDEN_EAS / f"{name}.txt"
    if os.environ.get("UPDATE_GOLDEN") or not file.exists():
        GOLDEN_EAS.mkdir(parents=True, exist_ok=True)
        file.write_bytes((text + "\n").encode("utf-8"))
    assert text + "\n" == file.read_bytes().decode("utf-8")


# ---------------------------------------------------------------------------------------------------------------------
# Reading: golden trees
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["custom-properties-single", "custom-properties-allday", "imported-single", "nextcloud-web", "with-attendees", "outlook-new", "duration", "imported-series", "imported-exception", "vtodo"]
)
def test_golden_tree(name):
    check_golden(name, output(golden(f"{name}.ics")))


def test_fixed_order_per_e32():
    k = output(golden("nextcloud-web.ics"))
    names = [tags.display(c).split(":")[1] for c in k.children]
    assert names == [
        "Timezone",
        "AllDayEvent",
        "BusyStatus",
        "Categories",
        "DtStamp",
        "EndTime",
        "Location",
        "MeetingStatus",
        "OrganizerEmail",
        "OrganizerName",
        "Reminder",
        "Sensitivity",
        "Subject",
        "StartTime",
        "UID",
        "Body",
    ]


def test_order_with_series_has_recurrence_before_reminder_and_exceptions_before_body():
    alarm = "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:x\r\nTRIGGER:-PT5M\r\nEND:VALARM\r\n"
    ics = golden("imported-exception.ics").replace("SUMMARY:Weekly start\r\n", f"SUMMARY:Weekly start\r\nDESCRIPTION:Text\r\n{alarm}", 1)
    ics = ics.replace("SUMMARY:Weekly start (later)\r\n", f"SUMMARY:Weekly start (later)\r\n{alarm}", 1)
    k = output(ics)
    names = [tags.display(c).split(":")[1] for c in k.children]
    assert names.index("OrganizerName") < names.index("Recurrence") < names.index("Reminder")
    assert names[-2:] == ["Exceptions", "Body"]


def test_root_is_applicationdata_and_everything_survives_the_wire():
    for name in ("custom-properties-single", "imported-single", "imported-exception", "nextcloud-web"):
        k = output(golden(f"{name}.ics"))
        assert tags.is_named(k, "AirSync:ApplicationData")
        assert wbxml.encode(wire(k)) == wbxml.encode(k)


def test_no_attendees_and_meetingstatus_zero_even_with_attendees():
    k = output(golden("with-attendees.ics"))
    assert tags.find(k, "Calendar:Attendees") is None
    assert value(k, "Calendar:MeetingStatus") == "0"
    assert value(k, "Calendar:OrganizerEmail") == "erika@example.test" and value(k, "Calendar:OrganizerName") == "Erika Sample"


def test_without_organizer_name_and_address_of_the_account_are_used():
    k = output(golden("custom-properties-single.ics"))
    assert value(k, "Calendar:OrganizerEmail") == ACCOUNT["account_address"] and value(k, "Calendar:OrganizerName") == ACCOUNT["account_name"]


def test_times_are_compact_datetime_in_utc():
    k = output(golden("imported-single.ics"))
    assert value(k, "Calendar:StartTime") == "20261103T130000Z" and value(k, "Calendar:EndTime") == "20261103T143000Z"
    assert value(k, "Calendar:DtStamp") == "20261029T120000Z"


def test_timezone_is_the_172_byte_block_of_the_appointment_zone():
    k = output(golden("imported-single.ics"))
    raw = timezone.blob_from_base64(value(k, "Calendar:Timezone"))
    assert len(raw) == 172 and timezone.iana_for_blob(raw, utc(2026, 11, 3)) == "Europe/Berlin"
    utc_ics = output(golden("custom-properties-single.ics"))
    assert timezone.iana_for_blob(timezone.blob_from_base64(value(utc_ics, "Calendar:Timezone")), utc(2026, 11, 2)) == "UTC"


def test_all_day_start_is_midnight_of_the_zone_and_end_exclusive():
    k = output(golden("custom-properties-allday.ics"))
    assert value(k, "Calendar:AllDayEvent") == "1"
    assert value(k, "Calendar:StartTime") == "20261104T230000Z" and value(k, "Calendar:EndTime") == "20261106T230000Z"
    assert timezone.iana_for_blob(timezone.blob_from_base64(value(k, "Calendar:Timezone")), utc(2026, 11, 5)) == "Europe/Berlin"


def test_all_day_without_end_lasts_one_day():
    ics = golden("custom-properties-allday.ics").replace("DTEND;VALUE=DATE:20261107\r\n", "")
    k = output(ics)
    assert value(k, "Calendar:EndTime") == "20261105T230000Z"


def test_all_day_with_other_default_zone():
    ics = golden("custom-properties-allday.ics")
    t = ical.read(ics, "America/New_York")[0]
    k = calendar.appointment_to_application_data(t, ics, body_types=(1,), trunc=None, body_max=1000, default_timezone="America/New_York", **ACCOUNT)
    assert value(k, "Calendar:StartTime") == "20261105T050000Z"


def test_busy_status_values():
    base = golden("custom-properties-single.ics")
    for extra, expected in (("TRANSP:TRANSPARENT", "0"), ("STATUS:TENTATIVE", "1"), ("X-MICROSOFT-CDO-BUSYSTATUS:OOF", "3"), ("X-MICROSOFT-CDO-BUSYSTATUS:WORKINGELSEWHERE", "2"), ("", "2")):
        ics = base.replace("SUMMARY:", f"{extra}\r\nSUMMARY:" if extra else "SUMMARY:", 1)
        assert value(output(ics), "Calendar:BusyStatus") == expected


def test_sensitivity_private_and_confidential():
    base = golden("custom-properties-single.ics")
    assert value(output(golden("imported-single.ics")), "Calendar:Sensitivity") == "2"
    assert value(output(base.replace("SUMMARY:", "CLASS:CONFIDENTIAL\r\nSUMMARY:", 1)), "Calendar:Sensitivity") == "3"
    assert value(output(base), "Calendar:Sensitivity") == "0"


def test_reminder_only_if_set():
    assert value(output(golden("custom-properties-single.ics")), "Calendar:Reminder") == "30"
    assert value(output(golden("custom-properties-allday.ics")), "Calendar:Reminder") == "900"
    assert tags.find(output(golden("imported-single.ics")), "Calendar:Reminder") is None
    assert tags.find(output(golden("duration.ics")), "Calendar:Reminder") is None


def test_reminder_comes_from_the_first_display_alarm_not_from_email():
    assert value(output(golden("nextcloud-web.ics")), "Calendar:Reminder") == "10"


def test_categories_as_children():
    k = output(golden("nextcloud-web.ics"))
    cats = tags.find(k, "Calendar:Categories")
    assert [c.text for c in tags.find_all(cats, "Calendar:Category")] == ["Work", "Planning"]
    assert tags.find(output(golden("custom-properties-single.ics")), "Calendar:Categories") is None


def test_location_is_on_page_4_not_in_airsyncbase():
    k = output(golden("custom-properties-single.ics"))
    location = tags.find(k, "Calendar:Location")
    assert location is not None and location.page == 4 and location.text == "Office Köln"
    assert tags.find(output(golden("duration.ics")), "Calendar:Location") is None


def test_umlauts_emoji_and_line_breaks_survive_the_wire():
    ics = (
        golden("custom-properties-single.ics")
        .replace("SUMMARY:Review Müller\\, Zürich & Co", "SUMMARY:Café 😀 Ångström ß")
        .replace("DESCRIPTION:First line\\nSecond line", "DESCRIPTION:Line 🎉\\nSecond ✓\\n\\nThird")
    )
    k = wire(output(ics))
    assert value(k, "Calendar:Subject") == "Café 😀 Ångström ß"
    body = tags.find(k, "AirSyncBase:Body")
    assert value(body, "AirSyncBase:Data") == "Line 🎉\r\nSecond ✓\r\n\r\nThird"
    assert value(body, "AirSyncBase:EstimatedDataSize") == str(len("Line 🎉\r\nSecond ✓\r\n\r\nThird".encode("utf-8")))


def test_uid_over_300_characters_is_not_delivered():
    long = golden("custom-properties-single.ics").replace("UID:external-0001", "UID:" + "u" * 301)
    assert output(long) is None
    tight = golden("custom-properties-single.ics").replace("UID:external-0001", "UID:" + "u" * 300)
    assert value(output(tight), "Calendar:UID") == "u" * 300


# ---------------------------------------------------------------------------------------------------------------------
# Reading: body
# ---------------------------------------------------------------------------------------------------------------------


def with_html() -> str:
    return golden("custom-properties-single.ics").replace("LOCATION:", "X-ALT-DESC;FMTTYPE=text/html:<p>First <b>line</b></p>\r\nLOCATION:", 1)


def body_from(k: Node) -> Node:
    return tags.find(k, "AirSyncBase:Body")


def test_body_order_of_the_children():
    body = body_from(output(golden("custom-properties-single.ics")))
    assert [tags.display(c) for c in body.children] == ["AirSyncBase:Type", "AirSyncBase:EstimatedDataSize", "AirSyncBase:Truncated", "AirSyncBase:Data"]


def test_html_only_with_bodypreference_2_and_existing_html():
    assert value(body_from(output(with_html(), body_types=(2, 1))), "AirSyncBase:Type") == "2"
    assert value(body_from(output(with_html(), body_types=(2, 1))), "AirSyncBase:Data") == "<p>First <b>line</b></p>"
    assert value(body_from(output(with_html(), body_types=(1,))), "AirSyncBase:Type") == "1"
    assert value(body_from(output(with_html(), body_types=())), "AirSyncBase:Type") == "1"
    assert value(body_from(output(golden("custom-properties-single.ics"), body_types=(2,))), "AirSyncBase:Type") == "1"


def test_type_3_and_4_are_never_delivered():
    for types in ((3,), (4,), (4, 3)):
        assert value(body_from(output(with_html(), body_types=types)), "AirSyncBase:Type") == "1"


def test_without_description_no_body():
    assert body_from(output(golden("duration.ics"))) is None


def test_html_only_without_text_yields_text_from_the_html():
    ics = golden("duration.ics").replace("SUMMARY:", "X-ALT-DESC;FMTTYPE=text/html:<p>Hello<br>World &amp; Co</p>\r\nSUMMARY:", 1)
    body = body_from(output(ics, body_types=(1,)))
    assert value(body, "AirSyncBase:Data") == "Hello\r\nWorld & Co"


def test_truncation_size_truncates_and_sets_truncated():
    ics = golden("custom-properties-single.ics").replace("First line\\nSecond line", "0123456789" * 10)
    body = body_from(output(ics, trunc=10))
    assert value(body, "AirSyncBase:Data") == "0123456789"
    assert value(body, "AirSyncBase:Truncated") == "1" and value(body, "AirSyncBase:EstimatedDataSize") == "100"


def test_truncation_size_zero_yields_empty_body_with_truncated():
    body = body_from(output(golden("custom-properties-single.ics"), trunc=0))
    assert value(body, "AirSyncBase:Data") == "" and value(body, "AirSyncBase:Truncated") == "1"


def test_truncation_does_not_tear_a_character():
    ics = golden("custom-properties-single.ics").replace("First line\\nSecond line", "😀😀😀😀😀")
    body = body_from(output(ics, trunc=6))
    assert value(body, "AirSyncBase:Data") == "😀" and value(body, "AirSyncBase:Truncated") == "1"
    assert value(body, "AirSyncBase:EstimatedDataSize") == "20"


def test_body_max_caps_in_characters():
    ics = golden("custom-properties-single.ics").replace("First line\\nSecond line", "abcdefghij")
    body = body_from(output(ics, body_max=4))
    assert value(body, "AirSyncBase:Data") == "abcd" and value(body, "AirSyncBase:Truncated") == "1"
    assert value(body_from(output(ics, body_max=10)), "AirSyncBase:Truncated") == "0"


# ---------------------------------------------------------------------------------------------------------------------
# Reading: not mappable
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["vtodo.ics", "broken.ics"])
def test_unrepresentable_is_invisible(name):
    assert output(golden(name)) is None


def test_several_independent_vevents_are_invisible():
    second = golden("duration.ics").split("BEGIN:VEVENT", 1)[1].split("END:VEVENT", 1)[0].replace("duration-0001", "other")
    ics = golden("custom-properties-single.ics").replace("END:VCALENDAR", f"BEGIN:VEVENT{second}END:VEVENT\r\nEND:VCALENDAR")
    assert output(ics) is None


@pytest.mark.parametrize("line", ["RDATE;TZID=Europe/Berlin:20261201T080000", "RRULE:FREQ=MONTHLY;BYDAY=MO,TU;BYSETPOS=1", "RRULE:FREQ=HOURLY;COUNT=3"])
def test_unmappable_series_is_invisible(line):
    ics = golden("imported-series.ics").replace("RRULE:FREQ=WEEKLY;BYDAY=MO", line)
    assert output(ics) is None


def test_series_with_unreadable_rrule_is_invisible_instead_of_single_appointment():
    ics = golden("custom-properties-single.ics").replace("LOCATION:", "RRULE:FREQ=WEEKLY;BYDAY=XX\r\nLOCATION:", 1)
    assert ical.read(ics)[0].representable  # ical only sees the single appointment
    assert output(ics) is None


def test_series_is_read_from_the_master_even_if_the_exception_comes_first():
    ics = golden("imported-exception.ics")
    master, exception = ics.split("BEGIN:VEVENT")[1:]
    swapped = ics.split("BEGIN:VEVENT")[0] + "BEGIN:VEVENT" + exception.replace("END:VCALENDAR\r\n", "") + "BEGIN:VEVENT" + master + "END:VCALENDAR\r\n"
    k = output(swapped)
    assert k is not None and value(k, "Calendar:Subject") == "Weekly start"
    assert value(k, "Calendar:StartTime") == "20261102T070000Z"


def test_series_yields_recurrence_and_exceptions():
    k = output(golden("imported-series.ics"))
    assert value(tags.find(k, "Calendar:Recurrence"), "Calendar:Type") == "1"
    exceptions = tags.find(k, "Calendar:Exceptions")
    assert len(tags.find_all(exceptions, "Calendar:Exception")) == 1


def test_same_output_as_series_module():
    ics = golden("imported-exception.ics")
    k = output(ics)
    s = recurrence.read_series(ics)
    assert tags.find(k, "Calendar:Recurrence") == s.repetition
    assert tags.find(k, "Calendar:Exceptions").children == s.exceptions


# ---------------------------------------------------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------------------------------------------------

WRITABLE = Permissions(read=True, change=True, delete=True, create=True, is_organizer=False)
READ_ONLY = Permissions(read=True, change=False, delete=False, create=False, is_organizer=False)


def test_permissions_single_appointment_in_writable_folder():
    t = ical.read(golden("custom-properties-single.ics"))[0]
    r = calendar.eas_permissions(WRITABLE, t, False)
    assert (r.read, r.change, r.delete) == (True, True, True)


def test_permissions_organizer_and_attendee_never_changeable():
    t = ical.read(golden("with-attendees.ics"))[0]
    r = calendar.eas_permissions(WRITABLE, t, False)
    assert r.read and not r.change and not r.delete


def test_permissions_read_only_folder():
    t = ical.read(golden("custom-properties-single.ics"))[0]
    r = calendar.eas_permissions(READ_ONLY, t, False)
    assert r.read and not r.change and not r.delete


def test_permissions_series_are_never_changeable_but_readable():
    t = ical.read(golden("imported-series.ics"))[0]
    r = calendar.eas_permissions(WRITABLE, t, True)
    assert (r.read, r.change, r.delete, r.is_organizer) == (True, False, False, False)
    assert r.create == WRITABLE.create


def test_permissions_without_read_permission_give_none():
    t = ical.read(golden("imported-series.ics"))[0]
    assert calendar.eas_permissions(NONE, t, True) == NONE
    assert calendar.eas_permissions(NONE, ical.read(golden("custom-properties-single.ics"))[0], False) == NONE


def test_permissions_unrepresentable_single_appointment():
    t = ical.read(golden("vtodo.ics"))[0]
    assert calendar.eas_permissions(WRITABLE, t, False) == NONE


# ---------------------------------------------------------------------------------------------------------------------
# Writing: helpers
# ---------------------------------------------------------------------------------------------------------------------


def compact(w: dt.datetime) -> str:
    return w.strftime("%Y%m%dT%H%M%SZ")


def field(name: str, text: str | None = None, *children: Node) -> Node:
    return tags.node(name, *children, text=text)


def data(*children: Node | None) -> Node:
    return tags.node("AirSync:ApplicationData", *children)


def body(text: str, kind: int = 1) -> Node:
    return field("AirSyncBase:Body", None, field("AirSyncBase:Type", str(kind)), field("AirSyncBase:Data", text))


def add_data(*, start=utc(2026, 11, 10, 9), end=utc(2026, 11, 10, 10), blob=BERLIN, all_day="0", extra: tuple[Node, ...] = ()) -> Node:
    return data(
        field("Calendar:Timezone", blob),
        field("Calendar:AllDayEvent", all_day),
        field("Calendar:StartTime", compact(start)),
        field("Calendar:EndTime", compact(end)),
        field("Calendar:Subject", "New appointment"),
        *extra,
    )


def as_new(k: Node, uid: str = "new-0001") -> tuple[str, list[ical.FieldChange], list[str]]:
    fields, ignored = calendar.application_data_to_changes(wire(k), new=True)
    return ical.new(uid, fields, NOW), fields, ignored


def as_change(ics: str, k: Node, *, with_current: bool = True) -> tuple[str, list[ical.FieldChange]]:
    current = ical.read(ics)[0] if with_current else None
    fields, _ = calendar.application_data_to_changes(wire(k), new=False, current=current)
    return ical.patch(ics, fields, NOW), fields


def replace(k: Node, name: str, new_text: str | None) -> Node:
    """Copy of `k` with a replaced, added (if it was missing) or, for None, removed child."""
    target = tags.tag(name)
    children = []
    found = False
    for c in k.children:
        if (c.page, c.token) == target:
            found = True
            if new_text is not None:
                children.append(tags.node(name, text=new_text))
        else:
            children.append(c)
    if not found and new_text is not None:
        children.append(tags.node(name, text=new_text))
    return Node(k.page, k.token, k.text, k.data, tuple(children))


# ---------------------------------------------------------------------------------------------------------------------
# Writing: Add via ical.create
# ---------------------------------------------------------------------------------------------------------------------


def test_add_single_appointment_complete():
    k = add_data(
        extra=(
            field("Calendar:Location", "Office Köln"),
            body("Line 1\r\nLine 2"),
            field("Calendar:BusyStatus", "1"),
            field("Calendar:Sensitivity", "2"),
            field("Calendar:Reminder", "15"),
            field("Calendar:Categories", None, field("Calendar:Category", "Work"), field("Calendar:Category", "Plan")),
            field("Calendar:UID", "new-0001"),
            field("Calendar:DtStamp", "20261101T120000Z"),
        )
    )
    ics, fields, ignored = as_new(k)
    assert ignored == []
    t = ical.read(ics)[0]
    assert t.uid == "new-0001" and t.subject == "New appointment" and t.location == "Office Köln"
    assert t.text == "Line 1\nLine 2"
    assert (t.start, t.end) == (utc(2026, 11, 10, 9), utc(2026, 11, 10, 10))
    assert t.zone == "Europe/Berlin" and not t.all_day
    assert t.free_occupied == "Tentative" and t.classification == "PRIVATE" and t.reminder_min == 15
    assert t.categories == ("Work", "Plan")
    assert "BEGIN:VTIMEZONE" in ics and "TZID=Europe/Berlin" in ics
    assert "X-DAVBRIDGE-CREATED-BY:outlook" in ics


def test_add_field_types_as_ics_map_expects_them():
    k = add_data(extra=(body("<p>x</p>", 2), field("Calendar:Sensitivity", "1"), field("Calendar:Reminder", "5"), field("Calendar:Categories", None, field("Calendar:Category", "A"))))
    fields, _ = calendar.application_data_to_changes(k, new=True)
    after = {f.field_uri: f for f in fields}
    assert after["item:Subject"].value == "New appointment"
    assert after["item:Body"].value == ("html", "<p>x</p>")
    assert after["calendar:Start"].value == utc(2026, 11, 10, 9) and after["calendar:Start"].value.tzinfo is not None
    assert after["calendar:IsAllDayEvent"].value is False
    assert after["calendar:StartTimeZone"].value == "Europe/Berlin" and after["calendar:EndTimeZone"].value == "Europe/Berlin"
    assert after["item:Sensitivity"].value == "Personal"
    assert after["item:Categories"].value == ("A",)
    assert after["item:ReminderIsSet"].value is True and after["item:ReminderMinutesBeforeStart"].value == 5
    assert all(f.kind == "set" for f in fields)
    assert [f.field_uri for f in fields].index("item:ReminderIsSet") < [f.field_uri for f in fields].index("item:ReminderMinutesBeforeStart")


def test_add_html_body_keeps_x_alt_desc():
    ics, _, _ = as_new(add_data(extra=(body("<p>Fett <b>x</b></p>", 2),)))
    ev = vevent(ics)
    assert str(ev["X-ALT-DESC"]) == "<p>Fett <b>x</b></p>" and "Fett" in str(ev["DESCRIPTION"])


def test_add_umlauts_emoji_line_breaks():
    k = add_data(extra=(field("Calendar:Location", "Zürich 🏔"), body("Café 😀\r\nSecond line ß\r\n\r\nEnd")))
    k = replace(k, "Calendar:Subject", "Müller 😀 Zürich")
    ics, _, _ = as_new(k)
    t = ical.read(ics)[0]
    assert t.subject == "Müller 😀 Zürich" and t.location == "Zürich 🏔"
    assert t.text == "Café 😀\nSecond line ß\n\nEnd"


def test_add_private_and_confidential():
    for value_, classification in (("2", "PRIVATE"), ("3", "CONFIDENTIAL"), ("1", "PRIVATE")):
        ics, _, _ = as_new(add_data(extra=(field("Calendar:Sensitivity", value_),)))
        assert str(vevent(ics)["CLASS"]) == classification
    ics, _, _ = as_new(add_data(extra=(field("Calendar:Sensitivity", "0"),)))
    assert "CLASS" not in vevent(ics)


def test_add_without_reminder_has_no_alarm():
    ics, _, _ = as_new(add_data())
    assert not vevent(ics).walk("VALARM")


def test_add_foreign_zone_new_york_time_point_is_preserved():
    blob = timezone.blob_base64("America/New_York", utc(2026, 11, 10))
    ics, fields, _ = as_new(add_data(blob=blob, start=utc(2026, 11, 10, 14), end=utc(2026, 11, 10, 15)))
    assert {f.value for f in fields if f.field_uri.endswith("TimeZone")} == {"America/New_York"}
    t = ical.read(ics)[0]
    assert t.zone == "America/New_York" and (t.start, t.end) == (utc(2026, 11, 10, 14), utc(2026, 11, 10, 15))


def test_add_unknown_block_falls_back_to_berlin_time_point_is_kept():
    block = timezone.encode(timezone.Timezone(-137, timezone.NONE, 0, timezone.NONE, 0))
    ics, fields, _ = as_new(add_data(blob=block))
    assert {f.value for f in fields if f.field_uri.endswith("TimeZone")} == {"Europe/Berlin"}
    assert ical.read(ics)[0].start == utc(2026, 11, 10, 9)


def test_add_without_timezone_uses_berlin():
    ics, _, _ = as_new(add_data(blob=""))
    assert ical.read(ics)[0].zone == "Europe/Berlin"


def test_add_without_timezone_element_uses_berlin():
    k = data(field("Calendar:StartTime", "20261110T090000Z"), field("Calendar:EndTime", "20261110T100000Z"))
    ics, _, _ = as_new(k)
    assert ical.read(ics)[0].zone == "Europe/Berlin"


# ---------------------------------------------------------------------------------------------------------------------
# Writing: all-day
# ---------------------------------------------------------------------------------------------------------------------


def all_day_add(zone: str, start: dt.datetime, end: dt.datetime, blob: str | None = None) -> str:
    blob = blob or timezone.blob_base64(zone, start)
    ics, _, _ = as_new(add_data(start=start, end=end, blob=blob, all_day="1"))
    return ics


def test_all_day_berlin():
    ics = all_day_add("Europe/Berlin", utc(2026, 11, 4, 23), utc(2026, 11, 6, 23))
    ev = vevent(ics)
    assert str(ev["DTSTART"].to_ical().decode()) == "20261105" and ev["DTEND"].to_ical().decode() == "20261107"
    t = ical.read(ics)[0]
    assert t.all_day and (t.start, t.end) == (utc(2026, 11, 4, 23), utc(2026, 11, 6, 23))


def test_all_day_foreign_zone_tokyo_keeps_the_intended_day():
    ics = all_day_add("Asia/Tokyo", utc(2026, 11, 4, 15), utc(2026, 11, 5, 15))  # 5 Nov 00:00 to 6 Nov 00:00 JST
    ev = vevent(ics)
    assert ev["DTSTART"].to_ical().decode() == "20261105" and ev["DTEND"].to_ical().decode() == "20261106"


def test_all_day_foreign_zone_new_york():
    ics = all_day_add("America/New_York", utc(2026, 11, 5, 5), utc(2026, 11, 6, 5))
    ev = vevent(ics)
    assert ev["DTSTART"].to_ical().decode() == "20261105" and ev["DTEND"].to_ical().decode() == "20261106"


def test_all_day_zone_without_matching_iana_zone_keeps_the_day():
    block = timezone.encode(timezone.Timezone(-137, timezone.NONE, 0, timezone.NONE, 0))  # UTC+2:17 does not exist
    start = dt.datetime(2026, 11, 5, tzinfo=UTC) - dt.timedelta(minutes=137)
    ics = all_day_add("", start, start + dt.timedelta(days=1), blob=block)
    ev = vevent(ics)
    assert ev["DTSTART"].to_ical().decode() == "20261105" and ev["DTEND"].to_ical().decode() == "20261106"


def test_all_day_zone_with_dst_offset_without_iana_match():
    block = timezone.encode(timezone.Timezone(-137, timezone.Rule(10, 0, 5, 3, 0), 0, timezone.Rule(3, 0, 5, 2, 0), -60))
    start = dt.datetime(2026, 7, 10, tzinfo=UTC) - dt.timedelta(minutes=137 + 60)  # midnight at the daylight offset
    ics = all_day_add("", start, start + dt.timedelta(days=1), blob=block)
    assert vevent(ics)["DTSTART"].to_ical().decode() == "20260710"


def test_all_day_end_not_after_start_is_rejected():
    with pytest.raises(Rejected) as error:
        calendar.application_data_to_changes(add_data(start=utc(2026, 11, 4, 23), end=utc(2026, 11, 4, 23), all_day="1"), new=True)
    assert error.value.reason == "time"


def test_all_day_change_via_current_same_day_is_no_change():
    ics = golden("custom-properties-allday.ics")
    k = data(
        field("Calendar:Timezone", timezone.blob_base64("Asia/Tokyo", utc(2026, 11, 5))),
        field("Calendar:AllDayEvent", "1"),
        field("Calendar:StartTime", "20261104T150000Z"),
        field("Calendar:EndTime", "20261106T150000Z"),
    )
    _, fields = as_change(ics, k)
    assert fields == []


def test_all_day_change_other_day():
    ics = golden("custom-properties-allday.ics")
    k = data(field("Calendar:Timezone", BERLIN), field("Calendar:AllDayEvent", "1"), field("Calendar:StartTime", "20261105T230000Z"), field("Calendar:EndTime", "20261107T230000Z"))
    new_ics, _ = as_change(ics, k)
    ev = vevent(new_ics)
    assert ev["DTSTART"].to_ical().decode() == "20261106" and ev["DTEND"].to_ical().decode() == "20261108"
    assert [a for a in alarms(new_ics)] == alarms(ics)
    assert str(ev["X-EXAMPLE-PROTECTED-CREATED-BY"]) == "external-writer"


# ---------------------------------------------------------------------------------------------------------------------
# Writing: Change via ical.patch (data preservation)
# ---------------------------------------------------------------------------------------------------------------------


def complete_back(ics: str, **changes: str | None) -> Node:
    """The client sends the delivered object back and changes individual fields."""
    k = wire(output(ics))
    for name, text in changes.items():
        k = replace(k, f"Calendar:{name}", text)
    return k


def test_change_unchanged_object_yields_no_fields():
    for name in ("custom-properties-single", "custom-properties-allday", "imported-single", "nextcloud-web", "duration"):
        ics = golden(f"{name}.ics")
        _, fields = as_change(ics, complete_back(ics))
        assert fields == [], name


def test_change_subject_preserves_x_properties_and_alarm_and_metadata():
    ics = golden("custom-properties-single.ics")
    new_ics, fields = as_change(ics, complete_back(ics, Subject="New subject"))
    assert [f.field_uri for f in fields] == ["item:Subject"]
    old, new_ev = vevent(ics), vevent(new_ics)
    assert properties(new_ev, ONLY_METADATA | {"SUMMARY"}) == properties(old, ONLY_METADATA | {"SUMMARY"})
    assert str(new_ev["SUMMARY"]) == "New subject" and str(new_ev["X-EXAMPLE-PROTECTED-KEY"]) == "wa-key-4711"
    assert alarms(new_ics) == alarms(ics)
    assert int(str(new_ev["SEQUENCE"])) == 1


def test_change_preserves_second_alarm_vtimezone_and_categories():
    ics = golden("nextcloud-web.ics")
    new_ics, fields = as_change(ics, complete_back(ics, Location="Room 4"))
    assert [f.field_uri for f in fields] == ["calendar:Location"]
    assert alarms(new_ics) == alarms(ics)
    assert "BEGIN:VTIMEZONE" in new_ics and "TZID:Europe/Berlin" in new_ics
    assert ical.read(new_ics)[0].categories == ("Work", "Planning") and ical.read(new_ics)[0].location == "Room 4"
    assert len(vevent(new_ics).walk("VALARM")) == 2


def test_change_without_body_change_keeps_x_alt_desc():
    ics = with_html()
    new_ics, fields = as_change(ics, complete_back(ics, Subject="Other"))
    assert [f.field_uri for f in fields] == ["item:Subject"]
    assert str(vevent(new_ics)["X-ALT-DESC"]) == "<p>First <b>line</b></p>"
    assert ical.read(new_ics)[0].html == "<p>First <b>line</b></p>"


def test_change_body_text_new_replaces_description():
    ics = golden("custom-properties-single.ics")
    k = replace(complete_back(ics), "AirSyncBase:Body", None)
    k = Node(k.page, k.token, None, None, k.children + (body("Brand new\r\nSecond"),))
    new_ics, fields = as_change(ics, k)
    assert [f.field_uri for f in fields] == ["item:Body"]
    assert ical.read(new_ics)[0].text == "Brand new\nSecond"


def test_change_time_in_zone_of_the_appointment_not_in_blob_zone():
    ics = golden("imported-single.ics")
    k = complete_back(ics, StartTime="20261103T150000Z", EndTime="20261103T160000Z")
    k = replace(k, "Calendar:Timezone", timezone.blob_base64("Asia/Tokyo", utc(2026, 11, 3)))
    new_ics, fields = as_change(ics, k)
    assert {f.field_uri for f in fields} == {"calendar:Start", "calendar:End"}
    t = ical.read(new_ics)[0]
    assert t.zone == "Europe/Berlin" and (t.start, t.end) == (utc(2026, 11, 3, 15), utc(2026, 11, 3, 16))
    assert str(vevent(new_ics)["CLASS"]) == "PRIVATE" and str(vevent(new_ics)["TRANSP"]) == "TRANSPARENT"


def test_change_without_current_writes_all_sent_fields():
    ics = golden("custom-properties-single.ics")
    new_ics, fields = as_change(ics, complete_back(ics, Subject="Only Subject"), with_current=False)
    uris = {f.field_uri for f in fields}
    assert {"item:Subject", "calendar:Start", "calendar:End", "calendar:IsAllDayEvent", "item:Body"} <= uris
    assert "calendar:StartTimeZone" not in uris
    assert str(vevent(new_ics)["X-EXAMPLE-PROTECTED-CREATED-BY"]) == "external-writer" and alarms(new_ics) == alarms(ics)


def test_change_sensitivity_from_private_to_normal_removes_class():
    ics = golden("imported-single.ics")
    new_ics, fields = as_change(ics, complete_back(ics, Sensitivity="0"))
    assert [f.field_uri for f in fields] == ["item:Sensitivity"] and "CLASS" not in vevent(new_ics)


def test_change_private_unchanged_stays_private():
    ics = golden("imported-single.ics")
    _, fields = as_change(ics, complete_back(ics))
    assert fields == []


def test_change_reminder_changes_only_the_display_alarm():
    ics = golden("nextcloud-web.ics")
    new_ics, fields = as_change(ics, complete_back(ics, Reminder="45"))
    assert [f.field_uri for f in fields] == ["item:ReminderMinutesBeforeStart"]
    assert ical.read(new_ics)[0].reminder_min == 45
    assert len(vevent(new_ics).walk("VALARM")) == 2
    assert alarms(new_ics)[1] == alarms(ics)[1]


def test_change_empty_reminder_removes_only_the_display_alarm():
    ics = golden("nextcloud-web.ics")
    new_ics, fields = as_change(ics, complete_back(ics, Reminder=""))
    assert [f.field_uri for f in fields] == ["item:ReminderIsSet"]
    alarms_new = vevent(new_ics).walk("VALARM")
    assert len(alarms_new) == 1 and str(alarms_new[0]["ACTION"]) == "EMAIL"


def test_change_new_reminder_on_appointment_without_alarm():
    ics = golden("imported-single.ics")
    new_ics, _ = as_change(ics, complete_back(ics, Reminder="20"))
    assert ical.read(new_ics)[0].reminder_min == 20


def test_change_categories_empty_only_deletes_explicitly_empty():
    ics = golden("nextcloud-web.ics")
    k = replace(complete_back(ics), "Calendar:Categories", None)
    _, fields = as_change(ics, k)
    assert fields == []  # a missing element deletes nothing
    k = Node(k.page, k.token, None, None, k.children + (field("Calendar:Categories"),))
    new_ics, fields = as_change(ics, k)
    assert [(f.field_uri, f.kind) for f in fields] == [("item:Categories", "delete")]
    assert ical.read(new_ics)[0].categories == ()


def test_change_missing_fields_change_nothing_and_are_reported():
    ics = golden("nextcloud-web.ics")
    k = data(field("Calendar:Subject", "Only Subject"))
    new_ics, fields = as_change(ics, k)
    assert [f.field_uri for f in fields] == ["item:Subject"]
    assert ical.read(new_ics)[0].location == "Room 3"
    assert calendar.omitted_fields(k) == ["Calendar:Timezone", "Calendar:AllDayEvent", "Calendar:BusyStatus", "Calendar:DtStamp", "Calendar:EndTime", "Calendar:Sensitivity", "Calendar:StartTime"]
    assert calendar.omitted_fields(complete_back(ics)) == []


def test_change_empty_location_and_empty_subject_clear_the_field():
    ics = golden("nextcloud-web.ics")
    new_ics, fields = as_change(ics, data(field("Calendar:Location"), field("Calendar:Subject")))
    assert {(f.field_uri, f.kind) for f in fields} == {("calendar:Location", "delete"), ("item:Subject", "delete")}
    ev = vevent(new_ics)
    assert "LOCATION" not in ev and "SUMMARY" not in ev


def test_change_busy_status_free_sets_transp():
    ics = golden("custom-properties-single.ics")
    new_ics, _ = as_change(ics, complete_back(ics, BusyStatus="0"))
    assert str(vevent(new_ics)["TRANSP"]) == "TRANSPARENT"


def test_repetition_of_the_same_change_is_detected_as_applied_by_ics_map():
    ics = golden("custom-properties-single.ics")
    new_ics, fields = as_change(ics, complete_back(ics, Subject="Zweimal"))
    assert ical.already_applied(new_ics, fields)
    assert not ical.already_applied(ics, fields)


# ---------------------------------------------------------------------------------------------------------------------
# Writing: rejection, ignoring, allowlist
# ---------------------------------------------------------------------------------------------------------------------


def reason(k: Node, new: bool = True) -> str:
    with pytest.raises(Rejected) as error:
        calendar.application_data_to_changes(k, new=new)
    return error.value.reason


def test_attendees_with_content_are_rejected():
    attendee = field("Calendar:Attendees", None, field("Calendar:Attendee", None, field("Calendar:Email", "a@example.test"), field("Calendar:Name", "A")))
    assert reason(add_data(extra=(attendee,))) == "attendees"
    assert reason(data(attendee), new=False) == "attendees"


def test_empty_attendees_are_ignored():
    fields, ignored = calendar.application_data_to_changes(add_data(extra=(field("Calendar:Attendees"),)), new=True)
    assert fields and ignored == []


def test_recurrence_and_exceptions_are_rejected():
    assert reason(add_data(extra=(field("Calendar:Recurrence", None, field("Calendar:Type", "0"), field("Calendar:Interval", "1")),))) == "series"
    assert reason(add_data(extra=(field("Calendar:Exceptions", None, field("Calendar:Exception", None, field("Calendar:Deleted", "1"))),))) == "exception"
    assert reason(data(field("Calendar:Recurrence", None, field("Calendar:Type", "0"))), new=False) == "series"


@pytest.mark.parametrize("uid", ["a\x01b", "u" * 256])
def test_invalid_uid_is_rejected(uid):
    k = add_data(extra=(field("Calendar:UID", uid),))
    assert reason(k) == "uid"
    with pytest.raises(Rejected):
        calendar.application_data_uid(k)


def test_uid_is_delivered_separately_and_ignored_in_change():
    k = add_data(extra=(field("Calendar:UID", "client-uid-1"),))
    assert calendar.application_data_uid(k) == "client-uid-1"
    assert calendar.application_data_uid(add_data()) is None
    fields, ignored = calendar.application_data_to_changes(data(field("Calendar:UID", "x" * 400), field("Calendar:Subject", "S")), new=False)
    assert [f.field_uri for f in fields] == ["item:Subject"] and ignored == []
    assert not any("UID" in f.field_uri for f in calendar.application_data_to_changes(k, new=True)[0])


@pytest.mark.parametrize(
    "start, end",
    [
        ("20261110T100000Z", "20261110T090000Z"),
        ("20261110T090000", "20261110T100000Z"),
        ("2026-11-10T09:00:00Z", "20261110T100000Z"),
        ("20261310T090000Z", "20261110T100000Z"),
        ("", "20261110T100000Z"),
        ("10000101T000000Z", "20261110T100000Z"),
    ],
)
def test_invalid_times_are_rejected(start, end):
    k = data(field("Calendar:Timezone", BERLIN), field("Calendar:AllDayEvent", "0"), field("Calendar:StartTime", start), field("Calendar:EndTime", end))
    assert reason(k) == "time"


def test_add_without_start_or_end_is_rejected():
    assert reason(data(field("Calendar:Subject", "x"), field("Calendar:EndTime", "20261110T100000Z"))) == "time"
    assert reason(data(field("Calendar:Subject", "x"), field("Calendar:StartTime", "20261110T100000Z"))) == "time"
    assert reason(data(field("Calendar:Subject", "x"))) == "time"


def test_same_start_and_end_for_timed_appointments_is_allowed():
    fields, _ = calendar.application_data_to_changes(add_data(end=utc(2026, 11, 10, 9)), new=True)
    assert fields


def test_invalid_enumerations_are_rejected():
    assert reason(add_data(extra=(field("Calendar:BusyStatus", "9"),))) == "unknown-field"
    assert reason(add_data(extra=(field("Calendar:Sensitivity", "x"),))) == "unknown-field"
    assert reason(add_data(extra=(field("Calendar:Reminder", "-5"),))) == "unknown-field"
    assert reason(add_data(all_day="2")) == "time"


def test_neutral_fields_are_silently_ignored_unknown_ones_by_name():
    neutral = (
        field("Calendar:OrganizerName", "Wer"),
        field("Calendar:OrganizerEmail", "wer@example.test"),
        field("Calendar:MeetingStatus", "1"),
        field("Calendar:ResponseRequested", "1"),
        field("Calendar:DisallowNewTimeProposal", "1"),
        field("Calendar:ClientUid", "abc"),
        field("Calendar:AppointmentReplyTime", "20261101T000000Z"),
        field("Calendar:ResponseType", "1"),
        field("Calendar:DtStamp", "20261101T000000Z"),
    )
    fields, ignored = calendar.application_data_to_changes(add_data(extra=neutral), new=True)
    assert ignored == []
    unknown = (Node(4, 0x2B, "1"), field("Calendar:OnlineMeetingConfLink", "https://example.test"), field("Calendar:Body", "alt"))
    fields2, ignored2 = calendar.application_data_to_changes(add_data(extra=unknown), new=True)
    assert ignored2 == ["4:0x2B", "Calendar:OnlineMeetingConfLink", "Calendar:Body"]
    assert {f.field_uri for f in fields} == {f.field_uri for f in fields2}


def test_unknown_replaces_nothing_in_the_result_ics():
    unknown = (field("Calendar:OnlineMeetingConfLink", "https://example.test"), field("Calendar:OrganizerEmail", "wer@example.test"))
    ics, _, _ = as_new(add_data(extra=unknown))
    assert "example.test" not in ics and "ORGANIZER" not in ics and "ATTENDEE" not in ics


def test_body_type_rtf_or_mime_is_ignored_without_rejecting():
    fields, _ = calendar.application_data_to_changes(add_data(extra=(body("{\\rtf1}", 3),)), new=True)
    assert not any(f.field_uri == "item:Body" for f in fields)


def test_empty_body_in_change_clears_description():
    ics = golden("custom-properties-single.ics")
    new_ics, fields = as_change(ics, data(body("")))
    assert [(f.field_uri, f.kind) for f in fields] == [("item:Body", "delete")]
    assert "DESCRIPTION" not in vevent(new_ics)


def test_add_omits_empty_fields():
    k = add_data(extra=(field("Calendar:Location"), field("Calendar:Categories"), body("")))
    k = replace(k, "Calendar:Subject", "")
    fields, _ = calendar.application_data_to_changes(k, new=True)
    assert all(f.kind == "set" for f in fields)
    assert {"item:Body", "calendar:Location", "item:Categories"}.isdisjoint({f.field_uri for f in fields})
