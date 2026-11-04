"""EWS <-> iCalendar: reading, field table, patch without data loss, creation, idempotence. All data is synthetic."""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

import icalendar
import pytest
from ics_helpers import NOW, T_NS, alarms, delete, ews, golden, item_change, properties, set_field, utc, vevent
from lxml import etree

from davbridge import ical as m
from davbridge.config import FeatureSet
from davbridge.permissions import Permissions

LATER = dt.datetime(2026, 11, 2, 8, 30, tzinfo=dt.timezone.utc)
FLAG = m.ContextZone(iana="Europe/Berlin")
ONLY_METADATA = frozenset({"DTSTAMP", "LAST-MODIFIED", "SEQUENCE"})


def field_edit(uri, value, kind="set"):
    return m.FieldChange(uri, kind, value)


# ---------------------------------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------------------------------


def test_read_single_appointment_with_own_properties():
    t = m.read(golden("custom-properties-single.ics"))[0]
    assert t.representable and t.reason is None
    assert t.uid == "external-0001"
    assert t.subject == "Review Müller, Zürich & Co"
    assert t.location == "Office Köln"
    assert t.text == "First line\nSecond line"
    assert (t.start, t.end) == (utc(2026, 11, 2, 9), utc(2026, 11, 2, 10))
    assert t.zone == "UTC" and not t.all_day
    assert t.free_occupied == "Busy" and t.classification == "PUBLIC" and t.status == "CONFIRMED"
    assert t.reminder_min == 30
    assert not t.has_organizer and not t.has_attendee
    assert t.created == utc(2026, 10, 30, 8, 15)


def test_read_all_day_is_local_midnight_and_end_exclusive():
    t = m.read(golden("custom-properties-allday.ics"))[0]
    assert t.all_day and t.zone == "Europe/Berlin"
    assert (t.start, t.end) == (utc(2026, 11, 4, 23), utc(2026, 11, 6, 23))  # 5 Nov 00:00 to 7 Nov 00:00 Berlin
    assert t.reminder_min == 900


def test_read_all_day_with_other_default_zone():
    t = m.read(golden("custom-properties-allday.ics"), default_timezone="America/New_York")[0]
    assert t.start == utc(2026, 11, 5, 5) and t.zone == "America/New_York"


def test_read_imported_appointment_with_tzid_class_and_transparency():
    t = m.read(golden("imported-single.ics"))[0]
    assert (t.start, t.end) == (utc(2026, 11, 3, 13), utc(2026, 11, 3, 14, 30))
    assert t.zone == "Europe/Berlin"
    assert t.free_occupied == "Free" and t.classification == "PRIVATE" and t.sequence == 2
    assert t.location == "Clinic Dr. Muster, Hauptstraße 1"


def test_read_nextcloud_web_with_categories_and_two_alarms():
    t = m.read(golden("nextcloud-web.ics"))[0]
    assert t.categories == ("Work", "Planning")
    assert t.reminder_min == 10  # only the first DISPLAY alarm counts, not the EMAIL alarm
    assert t.representable


def test_read_duration_instead_of_dtend():
    t = m.read(golden("duration.ics"))[0]
    assert t.end - t.start == dt.timedelta(minutes=90)


def test_read_all_day_without_dtend_lasts_one_day():
    ics = golden("custom-properties-allday.ics").replace("DTEND;VALUE=DATE:20261107\r\n", "")
    t = m.read(ics)[0]
    assert t.end - t.start == dt.timedelta(days=1)


def test_read_attendees_and_organizer_stay_representable():
    t = m.read(golden("with-attendees.ics"))[0]
    assert t.representable and t.has_attendee and t.has_organizer
    assert t.attendee_display == ("Max Example", "gast@example.test")
    assert t.organizer == ("Erika Sample", "erika@example.test")


@pytest.mark.parametrize(
    "file, count, reason",
    [
        ("imported-series.ics", 1, "series"),
        ("imported-exception.ics", 2, "series"),
        ("vtodo.ics", 1, "vtodo"),
        ("broken.ics", 1, "invalid"),
    ],
)
def test_unrepresentable_carries_a_reason(file, count, reason):
    appointments = m.read(golden(file))
    assert len(appointments) == count
    assert all(not t.representable and t.reason == reason for t in appointments)
    assert m.representable(golden(file)) == (False, reason)


def test_two_vevents_without_series_are_several_vevents():
    ics = golden("duration.ics").replace("END:VCALENDAR", "BEGIN:VEVENT\r\nUID:zweiter\r\nDTSTAMP:20261030T081500Z\r\nDTSTART:20261111T100000Z\r\nEND:VEVENT\r\nEND:VCALENDAR")
    assert [t.reason for t in m.read(ics)] == ["several-vevent"] * 2


def test_broken_time_makes_only_this_appointment_invisible():
    ics = golden("duration.ics").replace("DTSTART:20261110T130000Z", "DTSTART:murks")
    t = m.read(ics)[0]
    assert not t.representable and t.reason == "invalid" and t.uid == "duration-0001"


def test_representable_function_for_normal_appointment():
    assert m.representable(golden("custom-properties-single.ics")) == (True, None)


def test_free_busy_from_busystatus_and_status():
    base = golden("duration.ics")
    for extra, expected in [
        ("X-MICROSOFT-CDO-BUSYSTATUS:OOF", "OOF"),
        ("X-MICROSOFT-CDO-BUSYSTATUS:TENTATIVE", "Tentative"),
        ("STATUS:TENTATIVE", "Tentative"),
        ("TRANSP:TRANSPARENT", "Free"),
        ("STATUS:CANCELLED", "Busy"),
    ]:
        ics = base.replace("SUMMARY:With duration", f"SUMMARY:With duration\r\n{extra}")
        assert m.read(ics)[0].free_occupied == expected, extra


def test_html_description_is_read():
    ics = golden("duration.ics").replace("SUMMARY:With duration", "SUMMARY:With duration\r\nDESCRIPTION:Hello\r\nX-ALT-DESC;FMTTYPE=text/html:<p>Hello</p>")
    t = m.read(ics)[0]
    assert t.html == "<p>Hello</p>" and t.text == "Hello"


# ---------------------------------------------------------------------------------------------------------------------
# Field table
# ---------------------------------------------------------------------------------------------------------------------

EXAMPLES = {
    "item:Subject": ("<t:Subject>Äpfel &amp; Birnen</t:Subject>", "Äpfel & Birnen"),
    "item:Body": ("<t:Body BodyType='HTML'>&lt;p&gt;Hi&lt;/p&gt;</t:Body>", ("html", "<p>Hi</p>")),
    "calendar:Location": ("<t:Location>Köln</t:Location>", "Köln"),
    "calendar:Start": ("<t:Start>2026-11-02T09:00:00Z</t:Start>", utc(2026, 11, 2, 9)),
    "calendar:End": ("<t:End>2026-11-02T10:30:00Z</t:End>", utc(2026, 11, 2, 10, 30)),
    "calendar:IsAllDayEvent": ("<t:IsAllDayEvent>true</t:IsAllDayEvent>", True),
    "calendar:StartTimeZone": ("<t:StartTimeZone Id='W. Europe Standard Time'/>", "Europe/Berlin"),
    "calendar:EndTimeZone": ("<t:EndTimeZone Id='W. Europe Standard Time'/>", "Europe/Berlin"),
    "calendar:MeetingTimeZone": ("<t:MeetingTimeZone TimeZoneName='W. Europe Standard Time'/>", "Europe/Berlin"),
    "calendar:LegacyFreeBusyStatus": ("<t:LegacyFreeBusyStatus>OOF</t:LegacyFreeBusyStatus>", "OOF"),
    "item:Sensitivity": ("<t:Sensitivity>Confidential</t:Sensitivity>", "Confidential"),
    "item:Categories": ("<t:Categories><t:String>A</t:String><t:String>B</t:String></t:Categories>", ("A", "B")),
    "item:ReminderIsSet": ("<t:ReminderIsSet>true</t:ReminderIsSet>", True),
    "item:ReminderMinutesBeforeStart": ("<t:ReminderMinutesBeforeStart>45</t:ReminderMinutesBeforeStart>", 45),
}


def fresh_appointment():
    return icalendar.Calendar.from_ical(golden("custom-properties-single.ics").replace("BEGIN:VALARM", "BEGIN:VALARM\r\nX-TEST:1").replace("TRIGGER:-PT30M", "TRIGGER:-PT30M")).walk("VEVENT")[0]


def test_table_has_an_example_for_every_writable_field():
    writable = {u for u, f in m.FIELDS.items() if f.classification is m.FieldClass.WRITE}
    assert writable == set(EXAMPLES)


def test_element_names_are_unique():
    names = [f.element for f in m.FIELDS.values() if f.element]
    assert len(names) == len(set(names))
    assert all(m.ELEMENT_TO_URI[f.element] == u for u, f in m.FIELDS.items() if f.element)


@pytest.mark.parametrize("uri", sorted(EXAMPLES))
def test_roundtrip_per_field(uri):
    """EWS -> value -> iCalendar -> value -> appointment -> EWS -> value; every step yields the same value."""
    field = m.FIELDS[uri]
    xml, expected = EXAMPLES[uri]
    flag = m.ContextZone(all_day=True) if uri == "calendar:IsAllDayEvent" else FLAG
    ev = fresh_appointment()
    value = field.from_ews(ews(xml), flag)
    assert value == expected
    field.in_ics(ev, value, flag)
    if uri in {"calendar:StartTimeZone", "calendar:EndTimeZone", "calendar:MeetingTimeZone"}:
        return  # pure time interpretation, no iCalendar field
    assert field.from_ics(ev) == value
    calendar = icalendar.Calendar()
    calendar.add_component(ev)
    appointment = m.read(calendar.to_ical().decode())[0]
    element = field.as_ews(appointment)
    assert element is not None
    assert field.from_ews(element, FLAG) == value


@pytest.mark.parametrize("uri", ["calendar:StartTimeZone", "calendar:EndTimeZone"])
def test_zone_fields_are_output_as_ews(uri):
    appointment = m.read(golden("imported-single.ics"))[0]
    element = m.FIELDS[uri].as_ews(appointment)
    assert element.get("Id") == "W. Europe Standard Time"
    assert m.FIELDS[uri].from_ews(element, FLAG) == "Europe/Berlin"


@pytest.mark.parametrize("uri", sorted(u for u, f in m.FIELDS.items() if f.classification is m.FieldClass.READ_ONLY))
def test_read_only_fields_are_rejected_on_write(uri):
    field = m.FIELDS[uri]
    change = item_change(set_field(uri, f"<t:{field.element}>x</t:{field.element}>"))
    with pytest.raises(m.FieldRejected) as error:
        m.read_changes(change, FeatureSet.DEFAULT, FLAG)
    assert error.value.reason == "read-only"
    with pytest.raises(m.FieldRejected):
        m.read_changes(item_change(delete(uri)), FeatureSet.DEFAULT, FLAG)


# ---------------------------------------------------------------------------------------------------------------------
# reading EWS changes (all or nothing)
# ---------------------------------------------------------------------------------------------------------------------


def test_changes_times_in_the_given_zone():
    change = item_change(
        set_field("item:Subject", "<t:Subject>New</t:Subject>"),
        set_field("calendar:StartTimeZone", "<t:StartTimeZone Id='W. Europe Standard Time'/>"),
        set_field("calendar:Start", "<t:Start>2027-03-28T09:00:00.000</t:Start>"),
        set_field("calendar:End", "<t:End>2027-03-28T10:00:00.000</t:End>"),
    )
    result = {a.field_uri: a.value for a in m.read_changes(change, FeatureSet.BASIC, FLAG)}
    assert result["item:Subject"] == "New"
    assert result["calendar:Start"] == utc(2027, 3, 28, 7)  # naive 09:00 in Berlin, daylight saving time
    assert result["calendar:End"] == utc(2027, 3, 28, 8)
    assert result["calendar:StartTimeZone"] == "Europe/Berlin"


def test_changes_naive_time_without_zone_uses_context_zone():
    change = item_change(set_field("calendar:Start", "<t:Start>2026-11-02T09:00:00</t:Start>"))
    result = m.read_changes(change, FeatureSet.BASIC, m.ContextZone("America/New_York"))
    assert result[0].value == utc(2026, 11, 2, 14)


def test_changes_time_with_offset():
    change = item_change(set_field("calendar:End", "<t:End>2026-11-02T10:00:00+01:00</t:End>"))
    assert m.read_changes(change, FeatureSet.BASIC, FLAG)[0].value == utc(2026, 11, 2, 9)


def test_changes_delete_of_an_optional_field():
    result = m.read_changes(item_change(delete("calendar:Location")), FeatureSet.BASIC, FLAG)
    assert result == [m.FieldChange("calendar:Location", "delete", None)]


@pytest.mark.parametrize("uri", ["calendar:Start", "calendar:End", "calendar:IsAllDayEvent", "calendar:StartTimeZone"])
def test_required_fields_cannot_be_deleted(uri):
    with pytest.raises(m.FieldRejected):
        m.read_changes(item_change(delete(uri)), FeatureSet.DEFAULT, FLAG)


@pytest.mark.parametrize(
    "update, reason",
    [
        (set_field("calendar:Recurrence", "<t:Recurrence><t:DailyRecurrence><t:Interval>1</t:Interval></t:DailyRecurrence></t:Recurrence>"), "series"),
        (set_field("calendar:RequiredAttendees", "<t:RequiredAttendees><t:Attendee/></t:RequiredAttendees>"), "attendees"),
        (set_field("calendar:OptionalAttendees", "<t:OptionalAttendees/>"), "attendees"),
        (set_field("item:Attachments", "<t:Attachments/>"), "attachments"),
        (set_field("item:Does-not-exist", "<t:Unknown/>"), "unknown-field"),
        (set_field("item:Importance", "<t:Importance>High</t:Importance>"), "unknown-field"),
        (set_field("calendar:CalendarItemType", "<t:CalendarItemType>RecurringMaster</t:CalendarItemType>"), "unknown-field"),
    ],
)
def test_unsupported_is_rejected_with_reason(update, reason):
    with pytest.raises(m.FieldRejected) as error:
        m.read_changes(item_change(update), FeatureSet.DEFAULT, FLAG)
    assert error.value.reason == reason


def test_one_unknown_field_rejects_the_whole_change():
    change = item_change(
        set_field("item:Subject", "<t:Subject>New</t:Subject>"),
        set_field("calendar:Recurrence", "<t:Recurrence/>"),
    )
    with pytest.raises(m.FieldRejected) as error:
        m.read_changes(change, FeatureSet.BASIC, FLAG)
    assert error.value.field_uri == "calendar:Recurrence" and error.value.reason == "series"


def test_extended_and_attached_fields_are_rejected():
    extended = ews(
        "<t:ItemChange><t:ItemId Id='a'/><t:Updates><t:SetItemField><t:ExtendedFieldURI PropertyTag='0x1' PropertyType='String'/><t:CalendarItem/></t:SetItemField></t:Updates></t:ItemChange>"
    )
    attached = ews("<t:ItemChange><t:ItemId Id='a'/><t:Updates><t:AppendToItemField><t:FieldURI FieldURI='item:Body'/><t:CalendarItem/></t:AppendToItemField></t:Updates></t:ItemChange>")
    for change in (extended, attached):
        with pytest.raises(m.FieldRejected):
            m.read_changes(change, FeatureSet.DEFAULT, FLAG)


def test_without_updates_or_without_value_is_value_error():
    with pytest.raises(ValueError):
        m.read_changes(ews("<t:ItemChange><t:ItemId Id='a'/></t:ItemChange>"), FeatureSet.BASIC, FLAG)
    with pytest.raises(ValueError):
        m.read_changes(item_change("<t:SetItemField><t:FieldURI FieldURI='item:Subject'/><t:CalendarItem/></t:SetItemField>"), FeatureSet.BASIC, FLAG)


@pytest.mark.parametrize(
    "uri, xml",
    [
        ("calendar:Start", "<t:Start>morgen</t:Start>"),
        ("calendar:IsAllDayEvent", "<t:IsAllDayEvent>vielleicht</t:IsAllDayEvent>"),
        ("calendar:LegacyFreeBusyStatus", "<t:LegacyFreeBusyStatus>Away</t:LegacyFreeBusyStatus>"),
        ("item:Body", "<t:Body BodyType='Rtf'>x</t:Body>"),
    ],
)
def test_uninterpretable_values_are_value_error(uri, xml):
    with pytest.raises(ValueError):
        m.read_changes(item_change(set_field(uri, xml)), FeatureSet.DEFAULT, FLAG)


def test_unknown_zone_is_key_error():
    with pytest.raises(KeyError):
        m.read_changes(item_change(set_field("calendar:StartTimeZone", "<t:StartTimeZone Id='Moon Standard Time'/>")), FeatureSet.BASIC, FLAG)


# --- feature sets -------------------------------------------------------------------------------------------------------


def test_default_set_fields_are_blocked_in_the_basic_set():
    for uri in ("item:Categories",):
        xml, _ = EXAMPLES[uri]
        with pytest.raises(m.FieldRejected) as error:
            m.read_changes(item_change(set_field(uri, xml)), FeatureSet.BASIC, FLAG)
        assert error.value.reason == "field-disabled"
    xml, _ = EXAMPLES["item:Sensitivity"]
    with pytest.raises(m.FieldRejected):
        m.read_changes(item_change(set_field("item:Sensitivity", xml)), FeatureSet.BASIC, FLAG)


def test_neutral_default_values_are_swallowed_in_the_basic_set_and_written_in_the_default_set():
    normal = item_change(
        set_field("item:Sensitivity", "<t:Sensitivity>Normal</t:Sensitivity>"),
        set_field("item:ReminderIsSet", "<t:ReminderIsSet>false</t:ReminderIsSet>"),
        set_field("item:Categories", "<t:Categories/>"),
    )
    assert {a.field_uri for a in m.read_changes(normal, FeatureSet.BASIC, FLAG)} == {"item:ReminderIsSet"}
    assert {a.field_uri for a in m.read_changes(normal, FeatureSet.DEFAULT, FLAG)} == {"item:Sensitivity", "item:ReminderIsSet", "item:Categories"}


def test_reminder_is_writable_in_the_basic_set():
    without = item_change(
        set_field("item:ReminderIsSet", "<t:ReminderIsSet>false</t:ReminderIsSet>"), set_field("item:ReminderMinutesBeforeStart", "<t:ReminderMinutesBeforeStart>15</t:ReminderMinutesBeforeStart>")
    )
    assert {a.field_uri for a in m.read_changes(without, FeatureSet.BASIC, FLAG)} == {"item:ReminderIsSet"}
    with_ = item_change(
        set_field("item:ReminderIsSet", "<t:ReminderIsSet>true</t:ReminderIsSet>"), set_field("item:ReminderMinutesBeforeStart", "<t:ReminderMinutesBeforeStart>15</t:ReminderMinutesBeforeStart>")
    )
    assert {a.field_uri for a in m.read_changes(with_, FeatureSet.BASIC, FLAG)} == {"item:ReminderIsSet", "item:ReminderMinutesBeforeStart"}


def test_deleting_a_default_set_field_in_the_basic_set_is_not_silently_ignored():
    with pytest.raises(m.FieldRejected):
        m.read_changes(item_change(delete("item:Categories")), FeatureSet.BASIC, FLAG)


def test_tolerated_default_values_without_effect():
    change = item_change(set_field("item:Importance", "<t:Importance>Normal</t:Importance>"), set_field("calendar:IsResponseRequested", "<t:IsResponseRequested>true</t:IsResponseRequested>"))
    assert m.read_changes(change, FeatureSet.BASIC, FLAG) == []


# ---------------------------------------------------------------------------------------------------------------------
# Reading a creation
# ---------------------------------------------------------------------------------------------------------------------

NEW_XML = """<t:CalendarItem>
  <t:ItemClass>IPM.Appointment</t:ItemClass><t:Subject>Dentist</t:Subject>
  <t:Sensitivity>Normal</t:Sensitivity><t:Body BodyType='Text'>Bitte pünktlich</t:Body>
  <t:ReminderIsSet>false</t:ReminderIsSet><t:UID>{uid}</t:UID>
  <t:Start>2026-11-12T14:00:00Z</t:Start><t:End>2026-11-12T15:00:00Z</t:End><t:IsAllDayEvent>false</t:IsAllDayEvent>
  <t:LegacyFreeBusyStatus>Busy</t:LegacyFreeBusyStatus><t:Location>Praxis</t:Location>
  <t:StartTimeZone Id='W. Europe Standard Time'/><t:EndTimeZone Id='W. Europe Standard Time'/>
  <t:RequiredAttendees/>
</t:CalendarItem>"""


def test_creation_reads_fields_and_uid_separately():
    uid, fields = m.read_creation(ews(NEW_XML.format(uid="outlook-uid-1")), FeatureSet.BASIC, FLAG)
    assert uid == "outlook-uid-1"
    assert {f.field_uri for f in fields} == {
        "item:Subject",
        "item:Body",
        "calendar:Start",
        "calendar:End",
        "calendar:IsAllDayEvent",
        "calendar:LegacyFreeBusyStatus",
        "calendar:Location",
        "calendar:StartTimeZone",
        "calendar:EndTimeZone",
        "item:ReminderIsSet",
    }
    assert all(f.kind == "set" for f in fields)


def test_creation_without_uid():
    uid, _ = m.read_creation(ews(NEW_XML.replace("<t:UID>{uid}</t:UID>", "")), FeatureSet.BASIC, FLAG)
    assert uid is None


@pytest.mark.parametrize(
    "extra, reason",
    [
        ("<t:Recurrence><t:DailyRecurrence/></t:Recurrence>", "series"),
        ("<t:OptionalAttendees><t:Attendee><t:Mailbox/></t:Attendee></t:OptionalAttendees>", "attendees"),
        ("<t:Attachments><t:FileAttachment/></t:Attachments>", "attachments"),
        ("<t:Something>x</t:Something>", "unknown-field"),
    ],
)
def test_creation_rejects_unsupported(extra, reason):
    xml = NEW_XML.format(uid="u").replace("</t:CalendarItem>", extra + "</t:CalendarItem>")
    with pytest.raises(m.FieldRejected) as error:
        m.read_creation(ews(xml), FeatureSet.DEFAULT, FLAG)
    assert error.value.reason == reason


def test_creation_with_reminder_in_the_basic_set():
    xml = NEW_XML.format(uid="u").replace("<t:ReminderIsSet>false</t:ReminderIsSet>", "<t:ReminderIsSet>true</t:ReminderIsSet><t:ReminderMinutesBeforeStart>15</t:ReminderMinutesBeforeStart>")
    _, fields = m.read_creation(ews(xml), FeatureSet.BASIC, FLAG)
    assert {"item:ReminderIsSet", "item:ReminderMinutesBeforeStart"} <= {f.field_uri for f in fields}
    ics = m.new("u", fields, dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc))
    assert m.read(ics)[0].reminder_min == 15


def test_creation_uid_with_control_chars_is_invalid():
    with pytest.raises(ValueError):
        m.read_creation(ews("<t:CalendarItem><t:UID>a\tb</t:UID></t:CalendarItem>"), FeatureSet.BASIC, FLAG)


# ---------------------------------------------------------------------------------------------------------------------
# Patch: lossless
# ---------------------------------------------------------------------------------------------------------------------


def test_patch_changes_only_the_subject_and_preserves_everything_else():
    old = golden("custom-properties-single.ics")
    new = m.patch(old, [field_edit("item:Subject", "New title")], LATER)
    before, afterwards = vevent(old), vevent(new)
    assert str(afterwards["SUMMARY"]) == "New title"
    except_ = ONLY_METADATA | {"SUMMARY"}
    assert properties(before, except_) == properties(afterwards, except_)  # X-EXAMPLE-PROTECTED-*, DESCRIPTION, DTSTART ... unchanged
    assert alarms(old) == alarms(new)  # VALARM unchanged
    assert afterwards["X-EXAMPLE-PROTECTED-CREATED-BY"] == "external-writer" and afterwards["X-EXAMPLE-PROTECTED-KEY"] == "wa-key-4711"
    assert int(str(afterwards["SEQUENCE"])) == 1
    assert afterwards.decoded("LAST-MODIFIED") == LATER and afterwards.decoded("DTSTAMP") == LATER
    assert [t.uid for t in m.read(new)] == ["external-0001"]


@pytest.mark.parametrize("file", ["custom-properties-single.ics", "custom-properties-allday.ics", "imported-single.ics", "nextcloud-web.ics", "with-attendees.ics", "duration.ics"])
def test_patch_preserves_unknown_in_every_format(file):
    old = golden(file)
    new = m.patch(old, [field_edit("calendar:Location", "Elsewhere")], LATER)
    except_ = ONLY_METADATA | {"LOCATION"}
    assert properties(vevent(old), except_) == properties(vevent(new), except_)
    assert alarms(old) == alarms(new)
    assert str(vevent(new)["LOCATION"]) == "Elsewhere"
    for type in ("VTIMEZONE",):
        assert len(icalendar.Calendar.from_ical(old).walk(type)) <= len(icalendar.Calendar.from_ical(new).walk(type))


@pytest.mark.parametrize("file", ["imported-single.ics", "nextcloud-web.ics"])
def test_patch_preserves_existing_vtimezone_unchanged(file):
    def zones(ics):
        result = []
        for z in icalendar.Calendar.from_ical(ics).walk("VTIMEZONE"):
            result.append((properties(z), sorted(properties(k) for k in z.subcomponents)))
        return result

    old = golden(file)
    new = m.patch(old, [field_edit("item:Subject", "x"), field_edit("calendar:Start", utc(2027, 3, 28, 7)), field_edit("calendar:End", utc(2027, 3, 28, 8))], LATER)
    assert zones(old) == zones(new)


def test_patch_preserves_attendees_and_organizer():
    new = m.patch(golden("with-attendees.ics"), [field_edit("item:Subject", "Renamed")], LATER)
    ev = vevent(new)
    assert len(ev["ATTENDEE"]) == 2 and str(ev["ORGANIZER"]).endswith("erika@example.test") and ev["X-CUSTOM-FELD"] == "bleibt"


def test_patch_delete_removes_only_the_named_field():
    old = golden("custom-properties-single.ics")
    new = m.patch(old, [field_edit("calendar:Location", None, "delete")], LATER)
    assert "LOCATION" not in vevent(new)
    except_ = ONLY_METADATA | {"LOCATION"}
    assert properties(vevent(old), except_) == properties(vevent(new), except_)


def test_patch_time_keeps_the_existing_zone_and_vtimezone():
    old = golden("imported-single.ics")
    new = m.patch(old, [field_edit("calendar:Start", utc(2027, 3, 28, 7)), field_edit("calendar:End", utc(2027, 3, 28, 8, 30))], LATER)
    assert "DTSTART;TZID=Europe/Berlin:20270328T090000" in new and "DTEND;TZID=Europe/Berlin:20270328T103000" in new
    assert len(icalendar.Calendar.from_ical(new).walk("VTIMEZONE")) == 1  # the existing one stays, no second one
    t = m.read(new)[0]
    assert (t.start, t.end) == (utc(2027, 3, 28, 7), utc(2027, 3, 28, 8, 30))


def test_patch_time_in_utc_stays_utc():
    new = m.patch(golden("custom-properties-single.ics"), [field_edit("calendar:Start", utc(2026, 11, 2, 8)), field_edit("calendar:End", utc(2026, 11, 2, 9))], LATER)
    assert "DTSTART:20261102T080000Z" in new and "TZID" not in new


def test_patch_timezone_change_adds_vtimezone_only_once():
    changes = [field_edit("calendar:StartTimeZone", "Europe/Berlin"), field_edit("calendar:Start", utc(2026, 11, 2, 8)), field_edit("calendar:End", utc(2026, 11, 2, 9))]
    new = m.patch(golden("custom-properties-single.ics"), changes, LATER)
    assert "DTSTART;TZID=Europe/Berlin:20261102T090000" in new
    assert [str(z["TZID"]) for z in icalendar.Calendar.from_ical(new).walk("VTIMEZONE")] == ["Europe/Berlin"]
    twice = m.patch(new, [field_edit("item:Subject", "x")], LATER)
    assert len(icalendar.Calendar.from_ical(twice).walk("VTIMEZONE")) == 1


def test_patch_duration_becomes_dtend():
    new = m.patch(golden("duration.ics"), [field_edit("calendar:Start", utc(2026, 11, 10, 14))], LATER)
    ev = vevent(new)
    assert "DURATION" not in ev and "DTEND" in ev
    t = m.read(new)[0]
    assert t.start == utc(2026, 11, 10, 14) and t.end == utc(2026, 11, 10, 14, 30)  # the old end time 14:30 stays (only the start was changed)


def test_patch_without_time_change_keeps_duration():
    new = m.patch(golden("duration.ics"), [field_edit("item:Subject", "x")], LATER)
    assert "DURATION" in vevent(new) and "DTEND" not in vevent(new)


def test_patch_end_before_start_is_value_error():
    with pytest.raises(ValueError):
        m.patch(golden("custom-properties-single.ics"), [field_edit("calendar:End", utc(2026, 11, 2, 8))], LATER)
    with pytest.raises(ValueError):
        m.patch(golden("custom-properties-allday.ics"), [field_edit("calendar:End", utc(2026, 11, 4, 23))], LATER)


def test_patch_unchangeable_is_value_error():
    for name in ("imported-series.ics", "imported-exception.ics", "vtodo.ics"):
        with pytest.raises(ValueError):
            m.patch(golden(name), [field_edit("item:Subject", "x")], LATER)


def test_patch_all_day_to_timed_appointment_and_back():
    to_time = m.patch(
        golden("custom-properties-allday.ics"),
        [
            field_edit("calendar:StartTimeZone", "Europe/Berlin"),
            field_edit("calendar:IsAllDayEvent", False),
            field_edit("calendar:Start", utc(2026, 11, 5, 8)),
            field_edit("calendar:End", utc(2026, 11, 5, 9)),
        ],
        LATER,
    )
    t = m.read(to_time)[0]
    assert not t.all_day and (t.start, t.end) == (utc(2026, 11, 5, 8), utc(2026, 11, 5, 9))
    back = m.patch(
        to_time,
        [
            field_edit("calendar:StartTimeZone", "Europe/Berlin"),
            field_edit("calendar:IsAllDayEvent", True),
            field_edit("calendar:Start", utc(2026, 11, 4, 23)),
            field_edit("calendar:End", utc(2026, 11, 5, 23)),
        ],
        LATER,
    )
    assert "DTSTART;VALUE=DATE:20261105" in back and "DTEND;VALUE=DATE:20261106" in back
    t = m.read(back)[0]
    assert t.all_day and (t.start, t.end) == (utc(2026, 11, 4, 23), utc(2026, 11, 5, 23))


def test_patch_only_all_day_switch_turns_an_appointment_into_a_whole_day():
    new = m.patch(golden("imported-single.ics"), [field_edit("calendar:IsAllDayEvent", True)], LATER)
    assert "DTSTART;VALUE=DATE:20261103" in new and "DTEND;VALUE=DATE:20261104" in new


def test_patch_html_body_sets_alt_desc_and_text_fallback():
    new = m.patch(golden("custom-properties-single.ics"), [field_edit("item:Body", ("html", "<p>First</p><p>Second &amp; third<br>line</p>"))], LATER)
    ev = vevent(new)
    assert str(ev["X-ALT-DESC"]) == "<p>First</p><p>Second &amp; third<br>line</p>" and ev["X-ALT-DESC"].params["FMTTYPE"] == "text/html"
    assert str(ev["DESCRIPTION"]) == "First\nSecond & third\nline"
    again_text = m.patch(new, [field_edit("item:Body", ("text", "Nur Text\r\nzweite Zeile"))], LATER)
    ev = vevent(again_text)
    assert "X-ALT-DESC" not in ev and str(ev["DESCRIPTION"]) == "Nur Text\nzweite Zeile"
    empty = m.patch(new, [field_edit("item:Body", None, "delete")], LATER)
    assert "DESCRIPTION" not in vevent(empty) and "X-ALT-DESC" not in vevent(empty)


def test_patch_free_busy_writes_transp_and_busystatus():
    new = m.patch(golden("custom-properties-single.ics"), [field_edit("calendar:LegacyFreeBusyStatus", "Free")], LATER)
    ev = vevent(new)
    assert str(ev["TRANSP"]) == "TRANSPARENT" and str(ev["X-MICROSOFT-CDO-BUSYSTATUS"]) == "FREE"
    assert m.read(new)[0].free_occupied == "Free"
    again = m.patch(new, [field_edit("calendar:LegacyFreeBusyStatus", "Tentative")], LATER)
    assert m.read(again)[0].free_occupied == "Tentative" and str(vevent(again)["TRANSP"]) == "OPAQUE"


def test_patch_special_chars_in_subject_survive_the_roundtrip():
    title = 'Müller, Zürich; "quoted" \\ Backslash — End'
    new = m.patch(golden("duration.ics"), [field_edit("item:Subject", title)], LATER)
    assert m.read(new)[0].subject == title


def test_patch_reminder_changes_only_the_first_display_alarm():
    old = golden("nextcloud-web.ics")
    new = m.patch(old, [field_edit("item:ReminderMinutesBeforeStart", 45)], LATER)
    before, afterwards = alarms(old), alarms(new)
    assert afterwards[1] == before[1]  # EMAIL alarm untouched
    assert m.read(new)[0].reminder_min == 45
    out = m.patch(old, [field_edit("item:ReminderIsSet", False)], LATER)
    assert len(vevent(out).walk("VALARM")) == 1 and str(vevent(out).walk("VALARM")[0]["ACTION"]) == "EMAIL"
    an = m.patch(golden("duration.ics"), [field_edit("item:ReminderIsSet", True)], LATER)
    assert m.read(an)[0].reminder_min == 15


def test_patch_categories_and_sensitivity():
    new = m.patch(golden("nextcloud-web.ics"), [field_edit("item:Categories", ("X", "Y")), field_edit("item:Sensitivity", "Private")], LATER)
    t = m.read(new)[0]
    assert t.categories == ("X", "Y") and t.classification == "PRIVATE"
    back = m.patch(new, [field_edit("item:Sensitivity", "Normal"), field_edit("item:Categories", (), "delete")], LATER)
    t = m.read(back)[0]
    assert t.categories == () and t.classification == "PUBLIC"


def test_patch_result_is_valid_and_has_crlf():
    new = m.patch(golden("custom-properties-single.ics"), [field_edit("item:Subject", "x")], LATER)
    assert new.endswith("END:VCALENDAR\r\n") and "\n" not in new.replace("\r\n", "")
    icalendar.Calendar.from_ical(new)


# --- already_applied / canonical / href --------------------------------------------------------------------------


def test_already_applied_detects_repetition():
    change = [field_edit("item:Subject", "New title"), field_edit("calendar:Location", "Place")]
    old = golden("custom-properties-single.ics")
    assert not m.already_applied(old, change)
    new = m.patch(old, change, LATER)
    assert m.already_applied(new, change)
    assert not m.already_applied(new, [field_edit("item:Subject", "Other Title")])


def test_already_applied_time_is_representation_independent():
    # The protected appointment is in UTC, the change names the same time with zone Berlin: same point in time => already applied
    change = [field_edit("calendar:StartTimeZone", "Europe/Berlin"), field_edit("calendar:Start", utc(2026, 11, 2, 9)), field_edit("calendar:End", utc(2026, 11, 2, 10))]
    assert m.already_applied(golden("custom-properties-single.ics"), change)


def test_already_applied_is_false_if_the_patch_would_fail():
    assert not m.already_applied(golden("imported-series.ics"), [field_edit("item:Subject", "x")])
    assert not m.already_applied(golden("custom-properties-single.ics"), [field_edit("calendar:End", utc(2020, 1, 1))])


def test_canonical_is_stable_and_order_independent():
    a = [field_edit("item:Subject", "A"), field_edit("calendar:Start", utc(2026, 11, 2, 9))]
    b = [field_edit("calendar:Start", dt.datetime(2026, 11, 2, 10, tzinfo=dt.timezone(dt.timedelta(hours=1)))), field_edit("item:Subject", "A")]
    assert m.canonical(a) == m.canonical(b)
    assert m.canonical(a) != m.canonical([field_edit("item:Subject", "B"), field_edit("calendar:Start", utc(2026, 11, 2, 9))])
    assert isinstance(m.canonical(a), bytes)


@pytest.mark.parametrize("uid", ["abc-123", "5b0c6e1c-0000-4000-8000-000000000001", "imported-0123abcd@example.com", "a.b_c-d"])
def test_href_for_normal_uid(uid):
    assert m.href_for_uid(uid) == f"{uid}.ics"


@pytest.mark.parametrize("uid", ["mit/Schrägstrich", "with space", "Ünïcode", "x" * 101, "../../etc", "", ".hidden", "a\nb"])
def test_href_for_unsuitable_uid_is_a_hash(uid):
    href = m.href_for_uid(uid)
    assert m.FILE_PATTERN.fullmatch(href) and len(href) == 44 and href == m.href_for_uid(uid)
    assert href != m.href_for_uid(uid + "x")


# ---------------------------------------------------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------------------------------------------------


def default_fields(**extra):
    fields = [
        field_edit("calendar:StartTimeZone", "Europe/Berlin"),
        field_edit("calendar:EndTimeZone", "Europe/Berlin"),
        field_edit("item:Subject", "Clock change Müller"),
        field_edit("calendar:Start", utc(2027, 3, 28, 7)),
        field_edit("calendar:End", utc(2027, 3, 28, 8)),
        field_edit("calendar:Location", "Köln"),
        field_edit("item:Body", ("text", "Line 1\nLine 2")),
    ]
    return fields + [field_edit(k, v) for k, v in extra.items()]


def test_new_matches_the_golden_format():
    assert m.new("outlook-new-0001", default_fields(), NOW) == golden("outlook-new.ics")


def test_new_has_required_attributes_and_no_attendees():
    ics = m.new("u-1", default_fields(), NOW)
    ev = vevent(ics)
    assert str(icalendar.Calendar.from_ical(ics)["PRODID"]) == m.PRODID
    assert str(ev["X-DAVBRIDGE-CREATED-BY"]) == "outlook" and str(ev["UID"]) == "u-1"
    assert "ORGANIZER" not in ev and "ATTENDEE" not in ev
    assert int(str(ev["SEQUENCE"])) == 0 and ev.decoded("DTSTAMP") == NOW and ev.decoded("CREATED") == NOW
    assert "DTSTART;TZID=Europe/Berlin:20270328T090000" in ics and "BEGIN:VTIMEZONE" in ics
    assert m.representable(ics) == (True, None)


def test_new_without_zone_takes_europe_berlin():
    ics = m.new("u-2", [field_edit("calendar:Start", utc(2026, 11, 2, 8)), field_edit("calendar:End", utc(2026, 11, 2, 9))], NOW)
    assert "DTSTART;TZID=Europe/Berlin:20261102T090000" in ics


def test_new_all_day_with_date_and_exclusive_end():
    ics = m.new(
        "u-3",
        [
            field_edit("calendar:StartTimeZone", "Europe/Berlin"),
            field_edit("calendar:IsAllDayEvent", True),
            field_edit("calendar:Start", utc(2026, 11, 4, 23)),
            field_edit("calendar:End", utc(2026, 11, 5, 23)),
        ],
        NOW,
    )
    assert "DTSTART;VALUE=DATE:20261105" in ics and "DTEND;VALUE=DATE:20261106" in ics and "VTIMEZONE" not in ics
    t = m.read(ics)[0]
    assert t.all_day and (t.start, t.end) == (utc(2026, 11, 4, 23), utc(2026, 11, 5, 23))


def test_new_in_utc_has_no_vtimezone():
    ics = m.new("u-4", [field_edit("calendar:StartTimeZone", "UTC"), field_edit("calendar:Start", utc(2026, 11, 2, 8)), field_edit("calendar:End", utc(2026, 11, 2, 9))], NOW)
    assert "DTSTART:20261102T080000Z" in ics and "VTIMEZONE" not in ics


def test_new_free_busy_categories_html():
    ics = m.new("u-5", default_fields(**{"calendar:LegacyFreeBusyStatus": "Free"}) + [field_edit("item:Body", ("html", "<b>x</b>"))], NOW)
    t = m.read(ics)[0]
    assert t.free_occupied == "Free" and t.html == "<b>x</b>" and t.text == "x"


def test_new_time_required_and_order():
    with pytest.raises(ValueError):
        m.new("u", [field_edit("item:Subject", "ohne Zeit")], NOW)
    with pytest.raises(ValueError):
        m.new("u", [field_edit("calendar:Start", utc(2026, 11, 2, 8))], NOW)
    with pytest.raises(ValueError):
        m.new("u", [field_edit("calendar:Start", utc(2026, 11, 2, 9)), field_edit("calendar:End", utc(2026, 11, 2, 8))], NOW)
    with pytest.raises(ValueError):
        m.new("u", [field_edit("calendar:IsAllDayEvent", True), field_edit("calendar:Start", utc(2026, 11, 2, 9)), field_edit("calendar:End", utc(2026, 11, 2, 9))], NOW)


def test_new_result_is_independent_of_the_field_order():
    fields = default_fields()
    assert m.new("u", fields, NOW) == m.new("u", list(reversed(fields)), NOW)


def test_new_from_ews_xml_end_to_end():
    uid, fields = m.read_creation(ews(NEW_XML.format(uid="outlook-uid-9")), FeatureSet.BASIC, FLAG)
    t = m.read(m.new(uid, fields, NOW))[0]
    assert (t.uid, t.subject, t.location, t.text) == ("outlook-uid-9", "Dentist", "Praxis", "Bitte pünktlich")
    assert (t.start, t.end, t.zone) == (utc(2026, 11, 12, 14), utc(2026, 11, 12, 15), "Europe/Berlin")


def test_update_from_ews_xml_end_to_end():
    change = item_change(set_field("item:Subject", "<t:Subject>Renamed</t:Subject>"), set_field("calendar:Location", "<t:Location/>"))
    fields = m.read_changes(change, FeatureSet.BASIC, FLAG)
    t = m.read(m.patch(golden("custom-properties-single.ics"), fields, LATER))[0]
    assert t.subject == "Renamed" and t.location == ""


# ---------------------------------------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------------------------------------


def render(name="custom-properties-single.ics", **extra):
    appointment = m.read(golden(name))[0]
    parameter = dict(
        item_id="ITEM",
        change_key="KEY",
        folder_id="FOLDER",
        folder_change_key="OKEY",
        permissions=Permissions(True, True, True, True, True),
        mailbox_name="Test User",
        mailbox_address="o@dav.example.com",
    )
    parameter.update(extra)
    return m.build_calendar_item(appointment, **parameter)


def children(element):
    return [etree.QName(k).localname for k in element]


def text_from(element, name):
    return element.find(f"{{{T_NS}}}{name}").text


def test_render_id_only():
    element = render(only_id=True)
    assert children(element) == ["ItemId"] and element[0].get("ChangeKey") == "KEY"


def test_render_fields_and_order():
    element = render()
    names = children(element)
    assert names[:5] == ["ItemId", "ParentFolderId", "ItemClass", "Subject", "Sensitivity"]
    assert names.index("Body") < names.index("Importance") < names.index("EffectiveRights") < names.index("UID") < names.index("Start")
    assert text_from(element, "Subject") == "Review Müller, Zürich & Co"
    assert text_from(element, "Start") == "2026-11-02T09:00:00Z" and text_from(element, "End") == "2026-11-02T10:00:00Z"
    assert text_from(element, "IsAllDayEvent") == "false" and text_from(element, "UID") == "external-0001"
    assert text_from(element, "IsOrganizer") == "true" and text_from(element, "MyResponseType") == "Organizer"
    assert element.find(f"{{{T_NS}}}StartTimeZone").get("Id") == "UTC"
    body = element.find(f"{{{T_NS}}}Body")
    assert body.get("BodyType") == "Text" and body.text == "First line\nSecond line"


def test_render_without_body():
    assert "Body" not in children(render(with_body=False))


def test_render_permissions_come_only_from_the_permissions_object():
    read_only = Permissions(True, False, False, False, False)
    element = render("imported-single.ics", permissions=read_only)
    permissions = {etree.QName(e).localname: e.text for e in element.find(f"{{{T_NS}}}EffectiveRights")}
    assert permissions["Modify"] == "false" and permissions["Delete"] == "false" and permissions["Read"] == "true"
    assert text_from(element, "IsOrganizer") == "false" and text_from(element, "MyResponseType") == "Unknown"
    assert element.find(f"{{{T_NS}}}StartTimeZone").get("Id") == "W. Europe Standard Time"


def test_render_html_body_and_organizer_of_the_appointment():
    element = render("with-attendees.ics", permissions=Permissions(True, False, False, False, False))
    assert text_from(element, "IsMeeting") == "true"
    mailbox = element.find(f"{{{T_NS}}}Organizer/{{{T_NS}}}Mailbox")
    assert mailbox.find(f"{{{T_NS}}}EmailAddress").text == "erika@example.test"
    ics = golden("duration.ics").replace("SUMMARY:With duration", "SUMMARY:With duration\r\nDESCRIPTION:Hello\r\nX-ALT-DESC;FMTTYPE=text/html:<p>Hello</p>")
    html = m.build_calendar_item(
        m.read(ics)[0], item_id="i", change_key="k", folder_id="o", folder_change_key="ok", permissions=Permissions(True, True, True, True, True), mailbox_name="n", mailbox_address="a"
    )
    assert html.find(f"{{{T_NS}}}Body").get("BodyType") == "HTML"


def test_render_escapes_user_values_instead_of_xml_injection():
    malicious = "</t:Subject><t:Injiziert>x</t:Injiziert>&<"
    ics = golden("duration.ics").replace("SUMMARY:With duration", "SUMMARY:" + malicious.replace(",", "\\,"))
    element = render_from(ics)
    serialized = etree.tostring(element)
    read_back = etree.fromstring(serialized)
    assert "Injiziert" not in [etree.QName(k).localname for k in read_back]
    assert read_back.find(f"{{{T_NS}}}Subject").text == malicious


def render_from(ics):
    return m.build_calendar_item(
        m.read(ics)[0], item_id="i", change_key="k", folder_id="o", folder_change_key="ok", permissions=Permissions(True, True, True, True, True), mailbox_name="n", mailbox_address="a"
    )


def test_render_reminder_in_the_basic_set():
    basic = render()
    assert text_from(basic, "ReminderIsSet") == "true" and text_from(basic, "ReminderMinutesBeforeStart") == "30"
    default = render(features=FeatureSet.DEFAULT)
    assert text_from(default, "ReminderIsSet") == "true" and text_from(default, "ReminderMinutesBeforeStart") == "30"


def test_render_categories_and_sensitivity_in_the_default_set():
    element = render("nextcloud-web.ics", features=FeatureSet.DEFAULT)
    assert [s.text for s in element.find(f"{{{T_NS}}}Categories")] == ["Work", "Planning"]
    private = render("imported-single.ics", features=FeatureSet.DEFAULT)
    assert text_from(private, "Sensitivity") == "Private"
    assert text_from(render("imported-single.ics"), "Sensitivity") == "Normal"


# ---------------------------------------------------------------------------------------------------------------------
# Purity (architecture rules)
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["ical.py", "timezones.py", "permissions.py"])
def test_pure_modules_import_no_io(module):
    source = (Path(__file__).resolve().parents[3] / "src" / "davbridge" / module).read_text(encoding="utf-8")
    forbidden = {"asyncio", "httpx", "requests", "urllib", "sqlite3", "socket", "http", "subprocess"}
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    assert not (found & forbidden)
    assert "time.sleep" not in source and "open(" not in source


# ---------------------------------------------------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["", "x", "BEGIN:VCALENDAR", "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nEND:VCALENDAR", "\x00\x01\x02", "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"])
def test_read_never_raises_and_patch_only_value_error(source):
    appointments = m.read(source)
    assert appointments and not any(t.representable for t in appointments)
    with pytest.raises(ValueError):
        m.patch(source, [field_edit("item:Subject", "x")], LATER)
    assert m.already_applied(source, [field_edit("item:Subject", "x")]) is False


def test_long_and_exotic_texts_survive_creation_and_patch():
    title = "Café 🎉 Müller — " + "very long " * 30
    text = "Line 1\n\nLine 3 with comma, semicolon; and \\ Backslash\n" + "lang " * 80
    ics = m.new("u-lang", default_fields() + [field_edit("item:Subject", title), field_edit("item:Body", ("text", text))], NOW)
    assert all(len(line) <= 76 for line in ics.split("\r\n"))  # folded
    t = m.read(ics)[0]
    assert t.subject == title and t.text == text
    t2 = m.read(m.patch(ics, [field_edit("calendar:Location", "Ü")], LATER))[0]
    assert t2.subject == title and t2.text == text and t2.location == "Ü"


def test_golden_files_stay_readable_after_empty_patch():
    for file in ("custom-properties-single.ics", "custom-properties-allday.ics", "imported-single.ics", "nextcloud-web.ics", "with-attendees.ics", "duration.ics"):
        old = golden(file)
        new = m.patch(old, [], LATER)
        assert properties(vevent(old), ONLY_METADATA) == properties(vevent(new), ONLY_METADATA)
        assert alarms(old) == alarms(new)
        a, b = m.read(old)[0], m.read(new)[0]
        assert (a.uid, a.subject, a.start, a.end, a.all_day, a.zone) == (b.uid, b.subject, b.start, b.end, b.all_day, b.zone)


# ------------------------------------------------------------------ tolerated neutral fields


def _item_change(xml_updates: str):
    from lxml import etree

    T = "http://schemas.microsoft.com/exchange/services/2006/types"
    return etree.fromstring(f'<t:ItemChange xmlns:t="{T}"><t:ItemId Id="x"/><t:Updates>{xml_updates}</t:Updates></t:ItemChange>')


def test_outlook_accompanying_fields_are_also_strict_without_effect():
    """Outlook sends AllowNewTimeProposal (on creation) and EnhancedLocation (next to Location); rejecting them would mean silent discarding in
    Outlook."""
    xml = NEW_XML.format(uid="u").replace("</t:CalendarItem>", "<t:AllowNewTimeProposal>true</t:AllowNewTimeProposal></t:CalendarItem>")
    _, fields = m.read_creation(ews(xml), FeatureSet.DEFAULT, FLAG)
    assert "calendar:AllowNewTimeProposal" not in {f.field_uri for f in fields}
    change = item_change(set_field("calendar:EnhancedLocation", "<t:EnhancedLocation><t:DisplayName>Raum</t:DisplayName></t:EnhancedLocation>"))
    assert m.read_changes(change, FeatureSet.DEFAULT, FLAG) == []


OLD_CREATED = "X-" + "BRUE" + "CKE-ERSTELLT"
OLD_ICS = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//example.com//old//EN",
        "BEGIN:VEVENT",
        "UID:alt-1",
        "DTSTAMP:20261101T120000Z",
        "DTSTART:20261201T090000Z",
        "DTEND:20261201T100000Z",
        "SUMMARY:Alt",
        f"{OLD_CREATED}:outlook",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
)


def test_prodid_and_created_by_are_neutral():
    assert m.PRODID == "-//DAVbridge contributors//DAVbridge//EN"
    ics = m.new("u-9", default_fields(), NOW)
    assert "X-DAVBRIDGE-CREATED-BY:outlook" in ics and OLD_CREATED not in ics


def test_old_created_property_is_read_and_preserved_when_patching():
    assert m.read(OLD_ICS)[0].representable and m.read(OLD_ICS)[0].subject == "Alt"
    new = m.patch(OLD_ICS, [field_edit("item:Subject", "New")], NOW)
    assert f"{OLD_CREATED}:outlook" in new and "SUMMARY:New" in new and "X-DAVBRIDGE-CREATED-BY" not in new
