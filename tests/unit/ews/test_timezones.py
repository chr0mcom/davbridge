"""Time zones: CLDR mapping, VTIMEZONE generation and the transition days."""

from __future__ import annotations

import datetime as dt

import icalendar
import pytest
from ics_helpers import utc

from davbridge import ical as m
from davbridge import timezones


def test_windows_to_iana_territory_001():
    assert timezones.iana_for_windows("W. Europe Standard Time") == "Europe/Berlin"
    assert timezones.iana_for_windows("Pacific Standard Time") == "America/Los_Angeles"
    assert timezones.iana_for_windows("Eastern Standard Time") == "America/New_York"
    assert timezones.iana_for_windows("UTC") == "UTC"


def test_iana_is_accepted_unchanged():
    assert timezones.iana_for_windows("Europe/Berlin") == "Europe/Berlin"
    assert timezones.iana_for_windows("America/New_York") == "America/New_York"


@pytest.mark.parametrize("identifier", ["Does Es Not Standard Time", "", "Europe/Nirgendwo", "../etc/passwd"])
def test_unknown_zone_is_keyerror(identifier):
    with pytest.raises(KeyError):
        timezones.iana_for_windows(identifier)


def test_iana_to_windows_also_via_non_001_types():
    assert timezones.windows_for_iana("Europe/Berlin") == "W. Europe Standard Time"
    assert timezones.windows_for_iana("Europe/Vienna") == "W. Europe Standard Time"
    assert timezones.windows_for_iana("Etc/UTC") == "UTC"
    assert timezones.windows_for_iana("UTC") == "UTC"
    with pytest.raises(KeyError):
        timezones.windows_for_iana("Europe/Nirgendwo")


def test_reverse_mapping_is_a_roundtrip_for_all_001_zones():
    table, _ = timezones._tables()
    for windows, iana in table.items():
        assert timezones.iana_for_windows(timezones.windows_for_iana(iana)) == iana, windows


def _components(vtz):
    return [(type(c).__name__, c.decoded("DTSTART"), c.decoded("TZOFFSETFROM"), c.decoded("TZOFFSETTO")) for c in vtz.subcomponents]


def test_vtimezone_berlin_contains_both_transition_days():
    vtz = timezones.vtimezone("Europe/Berlin", dt.date(2027, 3, 20), dt.date(2027, 11, 5))
    assert str(vtz["TZID"]) == "Europe/Berlin"
    components = _components(vtz)
    summer = [k for k in components if k[0] == "TimezoneDaylight" and k[1] == dt.datetime(2027, 3, 28, 2, 0)]
    winter = [k for k in components if k[0] == "TimezoneStandard" and k[1] == dt.datetime(2027, 10, 31, 3, 0)]
    assert summer and summer[0][2:] == (dt.timedelta(hours=1), dt.timedelta(hours=2))
    assert winter and winter[0][2:] == (dt.timedelta(hours=2), dt.timedelta(hours=1))


def test_vtimezone_without_transition_has_exactly_one_stage():
    vtz = timezones.vtimezone("UTC", dt.date(2027, 3, 20), dt.date(2027, 3, 21))
    assert len(vtz.subcomponents) == 1
    assert vtz.subcomponents[0].decoded("TZOFFSETTO") == dt.timedelta(0)


def test_vtimezone_half_hour_zone_and_southern_hemisphere():
    india = timezones.vtimezone("Asia/Kolkata", dt.date(2027, 1, 1), dt.date(2027, 2, 1))
    assert india.subcomponents[0].decoded("TZOFFSETTO") == dt.timedelta(hours=5, minutes=30)
    sydney = timezones.vtimezone("Australia/Sydney", dt.date(2027, 9, 1), dt.date(2027, 11, 1))
    dst_start = [k for k in _components(sydney) if k[0] == "TimezoneDaylight" and k[1].date() == dt.date(2027, 10, 3)]
    assert dst_start and dst_start[0][3] == dt.timedelta(hours=11)


def test_vtimezone_swapped_bounds_and_datetime_values_are_accepted():
    a = timezones.vtimezone("Europe/Berlin", dt.date(2027, 4, 1), dt.date(2027, 3, 1))
    b = timezones.vtimezone("Europe/Berlin", dt.datetime(2027, 3, 1, 12), dt.datetime(2027, 4, 1, 12))
    assert _components(a) == _components(b)


def test_vtimezone_unknown_zone():
    with pytest.raises(KeyError):
        timezones.vtimezone("Europe/Nirgendwo", dt.date(2027, 1, 1), dt.date(2027, 1, 2))


def test_vtimezone_is_valid_ical():
    vtz = timezones.vtimezone("Europe/Berlin", dt.date(2027, 3, 1), dt.date(2027, 4, 1))
    calendar = icalendar.Calendar()
    calendar.add_component(vtz)
    assert icalendar.Calendar.from_ical(calendar.to_ical()).walk("VTIMEZONE")


# --- transition days through the whole path: EWS UTC -> ICS with TZID -> read UTC time -----------------------


@pytest.mark.parametrize(
    "local_time_utc, local_expected",
    [
        (utc(2027, 3, 27, 8), "20270327T090000"),  # day before, CET
        (utc(2027, 3, 28, 7), "20270328T090000"),  # transition day, CEST
        (utc(2027, 10, 30, 7), "20271030T090000"),  # day before, CEST
        (utc(2027, 10, 31, 8), "20271031T090000"),  # transition day, CET
    ],
)
def test_nine_local_time_on_transition_days(local_time_utc, local_expected):
    fields = [
        m.FieldChange("calendar:StartTimeZone", "set", "Europe/Berlin"),
        m.FieldChange("calendar:Start", "set", local_time_utc),
        m.FieldChange("calendar:End", "set", local_time_utc + dt.timedelta(hours=1)),
    ]
    ics = m.new("tz-test", fields, local_time_utc)
    assert f"DTSTART;TZID=Europe/Berlin:{local_expected}" in ics
    read_back = m.read(ics)[0]
    assert read_back.start == local_time_utc and read_back.zone == "Europe/Berlin"


def test_appointment_over_midnight_and_foreign_zone():
    fields = [
        m.FieldChange("calendar:StartTimeZone", "set", "America/New_York"),
        m.FieldChange("calendar:Start", "set", utc(2027, 3, 14, 3, 30)),  # 22:30 New York (EST) on 13 March
        m.FieldChange("calendar:End", "set", utc(2027, 3, 14, 8, 30)),  # 04:30 New York (EDT) on 14 March, after the transition at 07:00Z
    ]
    ics = m.new("tz-ny", fields, utc(2027, 3, 1))
    assert "DTSTART;TZID=America/New_York:20270313T223000" in ics
    read_back = m.read(ics)[0]
    assert (read_back.start, read_back.end) == (utc(2027, 3, 14, 3, 30), utc(2027, 3, 14, 8, 30))
    assert "DTEND;TZID=America/New_York:20270314T043000" in ics
    assert "TZID:America/New_York" in ics
