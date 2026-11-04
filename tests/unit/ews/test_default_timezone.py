"""Default zone (`default_timezone`): the preset is UTC, a configured value applies to floating ICS times, the context zone and the EAS fallback."""

import copy
import datetime as dt

import pytest
from tests.conftest import TEST_CONFIG

from davbridge import ical
from davbridge.config import from_dict, runtime_default_zone, set_runtime_default_zone
from davbridge.eas import calendar as eas_calendar

PENDING = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//test//en",
        "BEGIN:VEVENT",
        "UID:schwebend-1",
        "DTSTAMP:20261101T120000Z",
        "DTSTART:20261201T090000",
        "DTEND:20261201T100000",
        "SUMMARY:Schwebend",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
)


@pytest.fixture(autouse=True)
def _reset():
    yield
    set_runtime_default_zone("Europe/Berlin")


def test_preset_is_utc():
    k = copy.deepcopy(TEST_CONFIG)
    k["server"].pop("default_timezone")
    assert from_dict(k).default_timezone == "UTC"
    set_runtime_default_zone("UTC")
    assert runtime_default_zone() == "UTC"
    assert ical.ContextZone().iana == "UTC"
    t = ical.read(PENDING)[0]
    assert t.start == dt.datetime(2026, 12, 1, 9, 0, tzinfo=dt.timezone.utc) and t.zone == "UTC"


@pytest.mark.parametrize("zone, utc_hour", [("Europe/Berlin", 8), ("America/New_York", 14), ("Asia/Tokyo", 0), ("UTC", 9)])
def test_configured_zone_determines_floating_times(zone, utc_hour):
    set_runtime_default_zone(zone)
    assert ical.ContextZone().iana == zone
    t = ical.read(PENDING)[0]
    assert t.start.astimezone(dt.timezone.utc).hour == utc_hour and t.zone == zone


def test_explicit_default_zone_beats_the_configured_one():
    set_runtime_default_zone("Asia/Tokyo")
    assert ical.read(PENDING, "UTC")[0].start.hour == 9


def test_unknown_zone_is_not_adopted():
    set_runtime_default_zone("Europe/Berlin")
    with pytest.raises(Exception):
        set_runtime_default_zone("Mars/Olympus")
    assert runtime_default_zone() == "Europe/Berlin"


def test_eas_midnight_of_the_default_zone():
    set_runtime_default_zone("America/New_York")
    assert eas_calendar._default_zones_midnight(dt.date(2026, 12, 1)) == dt.datetime(2026, 12, 1, 5, 0, tzinfo=dt.timezone.utc)
    set_runtime_default_zone("UTC")
    assert eas_calendar._default_zones_midnight(dt.date(2026, 12, 1)) == dt.datetime(2026, 12, 1, 0, 0, tzinfo=dt.timezone.utc)


async def test_services_apply_the_configured_zone(services_factory):
    await services_factory({"server": {"default_timezone": "Asia/Tokyo"}})
    assert runtime_default_zone() == "Asia/Tokyo"
