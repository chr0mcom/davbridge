"""Series for reading: Recurrence and Exceptions from RRULE/EXDATE/RECURRENCE-ID; anything not mappable yields None with a reason. All data is
synthetic."""

from __future__ import annotations

import datetime as dt
import itertools
from zoneinfo import ZoneInfo

import pytest
from dateutil import rrule
from ics_helpers import golden

from davbridge.eas import recurrence, tags
from davbridge.eas.wbxml import Node

BERLIN = ZoneInfo("Europe/Berlin")
UTC = dt.timezone.utc
START = "DTSTART;TZID=Europe/Berlin:20261102T080000"
END = "DTEND;TZID=Europe/Berlin:20261102T083000"


def ics(rrule_text: str | None = "FREQ=WEEKLY;BYDAY=MO", *, start: str = START, end: str = END, extra: str = "", further: str = "") -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Example//Test//EN", "BEGIN:VEVENT", "UID:series-test@example.test", "DTSTAMP:20261029T120000Z", start, end, "SUMMARY:Series"]
    if rrule_text:
        lines.append(f"RRULE:{rrule_text}")
    if extra:
        lines.append(extra)
    lines.append("END:VEVENT")
    if further:
        lines.append(further)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def kind(k: Node, name: str) -> str | None:
    return tags.text_from(k, f"Calendar:{name}")


def repetition(text: str, **kw) -> Node:
    s = recurrence.read_series(ics(text, **kw))
    assert s is not None, recurrence.is_mappable(ics(text, **kw))
    return s.repetition


def tree_text(k: Node, indentation: int = 0) -> str:
    line = " " * indentation + tags.display(k) + (f" = {k.text!r}" if k.text is not None else "")
    return "\n".join([line, *(tree_text(c, indentation + 2) for c in k.children)])


# ---------------------------------------------------------------------------------------------------------------------
# Golden ICS
# ---------------------------------------------------------------------------------------------------------------------


def test_imported_series_weekday_and_deleted_instance():
    s = recurrence.read_series(golden("imported-series.ics"))
    assert s is not None
    assert tree_text(s.repetition) == "\n".join(
        [
            "Calendar:Recurrence",
            "  Calendar:Type = '1'",
            "  Calendar:Interval = '1'",
            "  Calendar:DayOfWeek = '2'",
        ]
    )
    assert len(s.exceptions) == 1
    assert tree_text(s.exceptions[0]) == "\n".join(
        [
            "Calendar:Exception",
            "  Calendar:Deleted = '1'",
            "  Calendar:ExceptionStartTime = '20261109T070000Z'",
        ]
    )
    assert s.master is not None and s.master.uid == "imported-synthetic-series@example.com"


def test_imported_exception_overrides_instance():
    s = recurrence.read_series(golden("imported-exception.ics"))
    assert s is not None and len(s.exceptions) == 1
    exception = s.exceptions[0]
    assert kind(exception, "Deleted") == "0"
    assert kind(exception, "ExceptionStartTime") == "20261116T070000Z"
    assert kind(exception, "StartTime") == "20261116T080000Z"
    assert kind(exception, "EndTime") == "20261116T083000Z"
    assert kind(exception, "Subject") == "Weekly start (later)"
    assert kind(exception, "Location") == ""
    assert kind(exception, "AllDayEvent") == "0" and kind(exception, "BusyStatus") == "2" and kind(exception, "Sensitivity") == "0"
    assert kind(exception, "Reminder") is None
    assert s.master is not None and s.master.subject == "Weekly start"


def test_single_appointments_are_not_a_series_but_unproblematic():
    for name in ("custom-properties-single.ics", "imported-single.ics", "nextcloud-web.ics", "vtodo.ics"):
        assert recurrence.read_series(golden(name)) is None
        assert recurrence.is_mappable(golden(name)) == (True, None)


# ---------------------------------------------------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------------------------------------------------


def test_daily_with_interval_and_count():
    w = repetition("FREQ=DAILY;INTERVAL=2;COUNT=10")
    assert [kind(w, n) for n in ("Type", "Interval", "Occurrences", "DayOfWeek", "Until")] == ["0", "2", "10", None, None]


def test_weekly_several_days_bitmask():
    w = repetition("FREQ=WEEKLY;BYDAY=MO,WE,FR")
    assert kind(w, "Type") == "1" and kind(w, "DayOfWeek") == str(2 + 8 + 32)


@pytest.mark.parametrize("tag, bit", [("SU", 1), ("MO", 2), ("TU", 4), ("WE", 8), ("TH", 16), ("FR", 32), ("SA", 64)])
def test_weekday_bits_sunday_is_one(tag, bit):
    days = {"SU": "20261101", "MO": "20261102", "TU": "20261103", "WE": "20261104", "TH": "20261105", "FR": "20261106", "SA": "20261107"}
    w = repetition(f"FREQ=WEEKLY;BYDAY={tag}", start=f"DTSTART;TZID=Europe/Berlin:{days[tag]}T080000")
    assert kind(w, "DayOfWeek") == str(bit)


def test_weekly_without_byday_takes_the_weekday_of_the_start():
    w = repetition("FREQ=WEEKLY;INTERVAL=3")
    assert kind(w, "DayOfWeek") == "2" and kind(w, "Interval") == "3"


def test_weekly_interval_with_sunday_and_several_days_is_not_mappable():
    assert recurrence.is_mappable(ics("FREQ=WEEKLY;INTERVAL=2;BYDAY=SA,SU", start="DTSTART;TZID=Europe/Berlin:20261107T080000")) == (False, "week-sunday")


def test_weekly_interval_without_sunday_is_mappable():
    w = repetition("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH;WKST=SU")
    assert kind(w, "Interval") == "2" and kind(w, "DayOfWeek") == str(2 + 16)


def test_monthly_on_the_month_day():
    w = repetition("FREQ=MONTHLY;BYMONTHDAY=2")
    assert [kind(w, n) for n in ("Type", "DayOfMonth", "Interval", "WeekOfMonth")] == ["2", "2", "1", None]


def test_monthly_without_details_takes_the_day_of_the_start():
    assert kind(repetition("FREQ=MONTHLY;INTERVAL=3"), "DayOfMonth") == "2"


@pytest.mark.parametrize(
    "rule, start, week, bit",
    [
        ("FREQ=MONTHLY;BYDAY=1MO", "20261102", "1", 2),
        ("FREQ=MONTHLY;BYDAY=2TU", "20261110", "2", 4),
        ("FREQ=MONTHLY;BYDAY=-1FR", "20261127", "5", 32),
        ("FREQ=MONTHLY;INTERVAL=2;BYDAY=4SU", "20261122", "4", 1),
    ],
)
def test_monthly_nth_weekday(rule, start, week, bit):
    w = repetition(rule, start=f"DTSTART;TZID=Europe/Berlin:{start}T080000")
    assert kind(w, "Type") == "3" and kind(w, "WeekOfMonth") == week and kind(w, "DayOfWeek") == str(bit)


def test_yearly_on_the_date():
    w = repetition("FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=2")
    assert [kind(w, n) for n in ("Type", "MonthOfYear", "DayOfMonth")] == ["5", "11", "2"]


def test_yearly_without_details_takes_the_start_date():
    w = repetition("FREQ=YEARLY")
    assert [kind(w, n) for n in ("Type", "MonthOfYear", "DayOfMonth")] == ["5", "11", "2"]


def test_yearly_nth_weekday():
    w = repetition("FREQ=YEARLY;BYMONTH=11;BYDAY=1MO")
    assert [kind(w, n) for n in ("Type", "MonthOfYear", "WeekOfMonth", "DayOfWeek")] == ["6", "11", "1", "2"]


def test_order_of_the_recurrence_children():
    w = repetition("FREQ=YEARLY;BYMONTH=11;BYDAY=1MO;COUNT=4")
    assert [tags.display(c) for c in w.children] == ["Calendar:Type", "Calendar:Interval", "Calendar:DayOfWeek", "Calendar:WeekOfMonth", "Calendar:MonthOfYear", "Calendar:Occurrences"]


def test_unlimited_series_has_neither_count_nor_end():
    w = repetition("FREQ=DAILY")
    assert kind(w, "Occurrences") is None and kind(w, "Until") is None


# ---------------------------------------------------------------------------------------------------------------------
# COUNT and UNTIL
# ---------------------------------------------------------------------------------------------------------------------


def test_count_999_yes_1000_no():
    assert kind(repetition("FREQ=DAILY;COUNT=999"), "Occurrences") == "999"
    assert recurrence.is_mappable(ics("FREQ=DAILY;COUNT=1000")) == (False, "count")


def test_count_and_until_together_not():
    assert recurrence.is_mappable(ics("FREQ=DAILY;COUNT=3;UNTIL=20261231T000000Z")) == (False, "count-and-until")


def test_until_is_the_start_of_the_last_instance_not_the_raw_value():
    w = repetition("FREQ=WEEKLY;BYDAY=MO;UNTIL=20261130T235959Z")
    assert kind(w, "Until") == "20261130T070000Z"  # Monday 30 Nov 08:00 Berlin (CET)


def test_until_with_date_applies_until_end_of_day():
    w = repetition("FREQ=WEEKLY;BYDAY=MO;UNTIL=20261130")
    assert kind(w, "Until") == "20261130T070000Z"


def test_until_just_before_the_last_instance_takes_the_previous_one():
    w = repetition("FREQ=WEEKLY;BYDAY=MO;UNTIL=20261130T065959Z")  # 07:59:59 Berlin on 30 Nov lies before 08:00
    assert kind(w, "Until") == "20261123T070000Z"


def test_until_across_the_dst_boundary_is_correct_in_utc():
    start = "DTSTART;TZID=Europe/Berlin:20270322T080000"
    w = repetition("FREQ=WEEKLY;BYDAY=MO;UNTIL=20270410T000000Z", start=start, end="DTEND;TZID=Europe/Berlin:20270322T083000")
    assert kind(w, "Until") == "20270405T060000Z"  # 5 Apr 2027 08:00 CEST = 06:00 UTC, before that 22 Mar 08:00 CET = 07:00 UTC


def test_until_after_more_than_5000_steps_is_the_raw_value():
    w = repetition("FREQ=DAILY;UNTIL=20600101T000000Z")
    assert kind(w, "Until") == "20600101T000000Z"


def test_until_before_the_start_is_not_mappable():
    assert recurrence.is_mappable(ics("FREQ=DAILY;UNTIL=20260101T000000Z")) == (False, "until-before-start")


# ---------------------------------------------------------------------------------------------------------------------
# Not mappable: never a wrong series
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule, reason",
    [
        ("FREQ=HOURLY;COUNT=5", "freq-hourly"),
        ("FREQ=MINUTELY;COUNT=5", "freq-minutely"),
        ("FREQ=MONTHLY;BYDAY=MO,TU;BYSETPOS=1", "bysetpos"),
        ("FREQ=MONTHLY;BYMONTHDAY=2,15", "several-month-days"),
        ("FREQ=MONTHLY;BYMONTHDAY=-1", "negative-month-day"),
        ("FREQ=YEARLY;BYMONTH=11,12", "several-bymonth"),
        ("FREQ=YEARLY;BYWEEKNO=20", "byweekno"),
        ("FREQ=YEARLY;BYYEARDAY=100", "byyearday"),
        ("FREQ=DAILY;BYHOUR=8,9", "byhour"),
        ("FREQ=DAILY;BYDAY=MO", "daily-with-filter"),
        ("FREQ=MONTHLY;BYDAY=MO", "byday-without-order"),
        ("FREQ=MONTHLY;BYDAY=1MO,3MO", "several-byday"),
        ("FREQ=MONTHLY;BYDAY=-2MO", "byday-order"),
        ("FREQ=WEEKLY;BYDAY=1MO", "week-order"),
        ("FREQ=YEARLY;INTERVAL=2", "year-interval"),
        ("FREQ=YEARLY;BYDAY=1MO", "year-byday-without-month"),
        ("FREQ=YEARLY;BYMONTHDAY=2", "year-month-day-without-month"),
        ("FREQ=DAILY;INTERVAL=1000", "interval"),
        ("FREQ=WEEKLY;BYDAY=XX", "invalid"),
    ],
)
def test_unmappable_rules(rule, reason):
    assert recurrence.read_series(ics(rule)) is None
    assert recurrence.is_mappable(ics(rule)) == (False, reason)


def test_rdate_is_not_mappable():
    text = ics("FREQ=DAILY;COUNT=3", extra="RDATE;TZID=Europe/Berlin:20261201T080000")
    assert recurrence.is_mappable(text) == (False, "rdate")


def test_rdate_without_rrule_is_not_mappable():
    assert recurrence.is_mappable(ics(None, extra="RDATE;TZID=Europe/Berlin:20261201T080000")) == (False, "rdate")


def test_exdate_without_rrule_is_not_mappable():
    assert recurrence.is_mappable(ics(None, extra="EXDATE;TZID=Europe/Berlin:20261109T080000")) == (False, "without-rrule")


def test_month_days_above_28_are_not_delivered():
    assert recurrence.is_mappable(ics("FREQ=MONTHLY;BYMONTHDAY=31")) == (False, "month-day-over-28")
    assert recurrence.is_mappable(ics("FREQ=MONTHLY", start="DTSTART;TZID=Europe/Berlin:20261130T080000", end="DTEND;TZID=Europe/Berlin:20261130T083000")) == (False, "month-day-over-28")


def test_29_february_yearly_is_not_delivered():
    text = ics("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29", start="DTSTART;TZID=Europe/Berlin:20280229T080000", end="DTEND;TZID=Europe/Berlin:20280229T083000")
    assert recurrence.is_mappable(text) == (False, "leap-day")


def test_start_that_does_not_match_the_rule():
    assert recurrence.is_mappable(ics("FREQ=WEEKLY;BYDAY=TU")) == (False, "start-does-not-match")  # 2 Nov 2026 is a Monday


def test_exception_without_master():
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Example//Test//EN\r\nBEGIN:VEVENT\r\nUID:x@example.test\r\nDTSTAMP:20261029T120000Z\r\n"
        "RECURRENCE-ID;TZID=Europe/Berlin:20261116T080000\r\nDTSTART;TZID=Europe/Berlin:20261116T090000\r\n"
        "DTEND;TZID=Europe/Berlin:20261116T093000\r\nSUMMARY:Only exception\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    assert recurrence.read_series(text) is None
    assert recurrence.is_mappable(text) == (False, "exception-without-master")


def test_two_independent_vevents_with_series():
    second = "BEGIN:VEVENT\r\nUID:other@example.test\r\nDTSTAMP:20261029T120000Z\r\nDTSTART:20261103T090000Z\r\nDTEND:20261103T100000Z\r\nSUMMARY:Other\r\nEND:VEVENT"
    text = ics("FREQ=DAILY;COUNT=3").replace("END:VCALENDAR", second + "\r\nEND:VCALENDAR")
    assert recurrence.is_mappable(text) == (False, "several-uid")


def test_thisandfuture_is_not_mappable():
    further = (
        "BEGIN:VEVENT\r\nUID:series-test@example.test\r\nDTSTAMP:20261029T120000Z\r\n"
        "RECURRENCE-ID;RANGE=THISANDFUTURE;TZID=Europe/Berlin:20261116T080000\r\n"
        "DTSTART;TZID=Europe/Berlin:20261116T090000\r\nDTEND;TZID=Europe/Berlin:20261116T093000\r\nSUMMARY:Ab hier\r\nEND:VEVENT"
    )
    text = ics("FREQ=WEEKLY;BYDAY=MO").replace("END:VCALENDAR", further + "\r\nEND:VCALENDAR")
    assert recurrence.is_mappable(text) == (False, "range")


def test_exception_on_non_instance_is_not_mappable():
    further = (
        "BEGIN:VEVENT\r\nUID:series-test@example.test\r\nDTSTAMP:20261029T120000Z\r\n"
        "RECURRENCE-ID;TZID=Europe/Berlin:20261117T080000\r\n"  # Tuesday, no instance
        "DTSTART;TZID=Europe/Berlin:20261117T090000\r\nDTEND;TZID=Europe/Berlin:20261117T093000\r\nSUMMARY:Falsch\r\nEND:VEVENT"
    )
    text = ics("FREQ=WEEKLY;BYDAY=MO").replace("END:VCALENDAR", further + "\r\nEND:VCALENDAR")
    assert recurrence.is_mappable(text) == (False, "exception-without-instance")


def test_exception_without_reminder_for_master_with_reminder():
    alarm = "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:x\r\nTRIGGER:-PT10M\r\nEND:VALARM\r\n"
    further = (
        "BEGIN:VEVENT\r\nUID:series-test@example.test\r\nDTSTAMP:20261029T120000Z\r\n"
        "RECURRENCE-ID;TZID=Europe/Berlin:20261116T080000\r\n"
        "DTSTART;TZID=Europe/Berlin:20261116T090000\r\nDTEND;TZID=Europe/Berlin:20261116T093000\r\nSUMMARY:Ohne\r\nEND:VEVENT"
    )
    text = ics("FREQ=WEEKLY;BYDAY=MO", extra=alarm.rstrip("\r\n")).replace("END:VCALENDAR", further + "\r\nEND:VCALENDAR")
    assert recurrence.is_mappable(text) == (False, "exception-without-reminder")


def test_more_than_256_exceptions_are_not_mappable():
    days = ",".join(f"{(dt.date(2026, 11, 2) + dt.timedelta(days=i)):%Y%m%d}T080000" for i in range(257))
    text = ics("FREQ=DAILY", extra=f"EXDATE;TZID=Europe/Berlin:{days}")
    assert recurrence.is_mappable(text) == (False, "too-many-exceptions")
    exactly = ",".join(f"{(dt.date(2026, 11, 2) + dt.timedelta(days=i)):%Y%m%d}T080000" for i in range(256))
    assert len(recurrence.read_series(ics("FREQ=DAILY", extra=f"EXDATE;TZID=Europe/Berlin:{exactly}")).exceptions) == 256


def test_broken_inputs_never_yield_a_series():
    assert recurrence.is_mappable(golden("broken.ics")) == (False, "invalid")
    for text in ("", "not ical", "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nRRULE:FREQ=DAILY\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"):
        assert recurrence.read_series(text) is None


# ---------------------------------------------------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------------------------------------------------


def test_exdate_hitting_no_instance_is_discarded():
    s = recurrence.read_series(ics("FREQ=WEEKLY;BYDAY=MO", extra="EXDATE;TZID=Europe/Berlin:20261110T080000"))  # Tuesday
    assert s is not None and s.exceptions == ()


def test_exdate_in_utc_and_as_date():
    utc = recurrence.read_series(ics("FREQ=WEEKLY;BYDAY=MO", extra="EXDATE:20261109T070000Z"))
    assert utc is not None and [kind(a, "ExceptionStartTime") for a in utc.exceptions] == ["20261109T070000Z"]


def test_several_exdates_are_sorted_and_without_duplicates():
    extra = "EXDATE;TZID=Europe/Berlin:20261123T080000,20261109T080000\r\nEXDATE;TZID=Europe/Berlin:20261109T080000"
    s = recurrence.read_series(ics("FREQ=WEEKLY;BYDAY=MO", extra=extra))
    assert [kind(a, "ExceptionStartTime") for a in s.exceptions] == ["20261109T070000Z", "20261123T070000Z"]


def test_exception_and_exdate_on_same_instance_count_as_deleted():
    further = (
        "BEGIN:VEVENT\r\nUID:series-test@example.test\r\nDTSTAMP:20261029T120000Z\r\n"
        "RECURRENCE-ID;TZID=Europe/Berlin:20261109T080000\r\n"
        "DTSTART;TZID=Europe/Berlin:20261109T090000\r\nDTEND;TZID=Europe/Berlin:20261109T093000\r\nSUMMARY:Doppelt\r\nEND:VEVENT"
    )
    text = ics("FREQ=WEEKLY;BYDAY=MO", extra="EXDATE;TZID=Europe/Berlin:20261109T080000").replace("END:VCALENDAR", further + "\r\nEND:VCALENDAR")
    s = recurrence.read_series(text)
    assert s is not None and [kind(a, "Deleted") for a in s.exceptions] == ["1"]


def test_all_day_series_has_midnight_of_the_zone_as_exception_time():
    text = ics("FREQ=WEEKLY;BYDAY=TH", start="DTSTART;VALUE=DATE:20261105", end="DTEND;VALUE=DATE:20261106", extra="EXDATE;VALUE=DATE:20261112")
    s = recurrence.read_series(text)
    assert s is not None
    assert kind(s.repetition, "DayOfWeek") == "16"
    assert [kind(a, "ExceptionStartTime") for a in s.exceptions] == ["20261111T230000Z"]
    assert s.master.all_day


def test_series_in_utc_stays_in_utc():
    text = ics("FREQ=DAILY;COUNT=3;", start="DTSTART:20261102T080000Z", end="DTEND:20261102T090000Z", extra="EXDATE:20261103T080000Z")
    s = recurrence.read_series(text)
    assert s is not None and [kind(a, "ExceptionStartTime") for a in s.exceptions] == ["20261103T080000Z"]


# ---------------------------------------------------------------------------------------------------------------------
# Daylight saving time: the EAS parameters yield the same instances as dateutil with a zone-aware start
# ---------------------------------------------------------------------------------------------------------------------


def eas_instances(w: Node, start: dt.datetime, count: int) -> list[dt.datetime]:
    """Reads the EAS parameters back into a rule independently of the module (wall-clock time of the zone) and yields UTC instances."""
    type, interval = int(kind(w, "Type")), int(kind(w, "Interval"))
    bits = int(kind(w, "DayOfWeek") or 0)
    days = [rrule.SU, rrule.MO, rrule.TU, rrule.WE, rrule.TH, rrule.FR, rrule.SA]
    weekdays = [days[i] for i in range(7) if bits & (1 << i)]
    parts = {"dtstart": start.replace(tzinfo=None), "interval": interval}
    if type == 0:
        parts["freq"] = rrule.DAILY
    elif type == 1:
        parts.update(freq=rrule.WEEKLY, byweekday=weekdays)
    elif type == 2:
        parts.update(freq=rrule.MONTHLY, bymonthday=int(kind(w, "DayOfMonth")))
    elif type == 3:
        week = int(kind(w, "WeekOfMonth"))
        parts.update(freq=rrule.MONTHLY, byweekday=weekdays[0](-1 if week == 5 else week))
    if kind(w, "Occurrences"):
        parts["count"] = int(kind(w, "Occurrences"))
    rule = rrule.rrule(**parts)
    return [x.replace(tzinfo=BERLIN).astimezone(UTC) for x in itertools.islice(rule, count)]


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=WEEKLY;BYDAY=MO;COUNT=8",
        "FREQ=DAILY;COUNT=12",
        "FREQ=WEEKLY;BYDAY=MO,SA;COUNT=10",
        "FREQ=MONTHLY;BYDAY=-1SU;COUNT=3",
    ],
)
def test_series_over_the_last_march_week_matches_dateutil(rule):
    start = dt.datetime(2027, 3, 15, 8, 0, tzinfo=BERLIN) if "MONTHLY" not in rule else dt.datetime(2027, 2, 28, 8, 0, tzinfo=BERLIN)
    text = ics(rule, start=f"DTSTART;TZID=Europe/Berlin:{start:%Y%m%dT%H%M%S}", end=f"DTEND;TZID=Europe/Berlin:{start:%Y%m%d}T083000")
    w = recurrence.read_series(text).repetition
    reference = rrule.rrulestr(rule, dtstart=start)
    expected = [x.astimezone(UTC) for x in reference]
    assert eas_instances(w, start, len(expected)) == expected


def test_dst_instances_by_hand():
    start = dt.datetime(2027, 3, 22, 8, 0, tzinfo=BERLIN)
    w = recurrence.read_series(ics("FREQ=WEEKLY;BYDAY=MO;COUNT=2", start="DTSTART;TZID=Europe/Berlin:20270322T080000", end="DTEND;TZID=Europe/Berlin:20270322T083000")).repetition
    times = eas_instances(w, start, 2)
    assert times == [dt.datetime(2027, 3, 22, 7, 0, tzinfo=UTC), dt.datetime(2027, 3, 29, 6, 0, tzinfo=UTC)]


def test_exdate_after_the_transition_has_dst_in_utc():
    text = ics("FREQ=WEEKLY;BYDAY=MO", start="DTSTART;TZID=Europe/Berlin:20270322T080000", end="DTEND;TZID=Europe/Berlin:20270322T083000", extra="EXDATE;TZID=Europe/Berlin:20270329T080000")
    s = recurrence.read_series(text)
    assert [kind(a, "ExceptionStartTime") for a in s.exceptions] == ["20270329T060000Z"]
