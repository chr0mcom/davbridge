"""Time zones: Windows zone id <-> IANA (CLDR, pinned under data/) and VTIMEZONE generation. No network, no asyncio."""

from __future__ import annotations

import datetime as dt
import functools
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar

FILE = "windowsZones.xml"
UTC_NAMES = frozenset({"UTC", "Etc/UTC", "Etc/GMT", "GMT", "Etc/UCT", "Etc/Universal", "Universal", "Zulu", "Etc/Zulu"})
STEP_S = 6 * 3600


def _data_path() -> Path:
    package = Path(__file__).resolve().parent
    for candidate in (package.parent / "data", package.parent.parent / "data", package / "data"):
        if (candidate / FILE).is_file():
            return candidate / FILE
    raise FileNotFoundError(FILE)


@functools.lru_cache(maxsize=1)
def _tables() -> tuple[Mapping[str, str], Mapping[str, str]]:
    """(windows -> iana for territory 001, iana -> windows). Immutable, loaded once."""
    root = ET.parse(_data_path()).getroot()
    win_iana: dict[str, str] = {}
    iana_win: dict[str, str] = {}
    for zone in root.iter("mapZone"):
        windows = zone.get("other", "")
        types = (zone.get("type") or "").split()
        if not windows or not types:
            continue
        if zone.get("territory") == "001":
            win_iana[windows] = types[0]
        for type in types:
            iana_win.setdefault(type, windows)
    win_iana["UTC"] = "UTC"
    for name in UTC_NAMES:
        iana_win[name] = "UTC"
    return MappingProxyType(win_iana), MappingProxyType(iana_win)


def iana_for_windows(windows_id: str) -> str:
    """Windows id -> IANA (territory 001). An IANA identifier that zoneinfo knows is accepted unchanged. Otherwise KeyError."""
    table, _ = _tables()
    if windows_id in table:
        return table[windows_id]
    if "/" in windows_id or windows_id in UTC_NAMES:
        try:
            ZoneInfo(windows_id)
        except (ZoneInfoNotFoundError, ValueError, OSError) as error:
            raise KeyError(windows_id) from error
        return windows_id
    raise KeyError(windows_id)


def windows_for_iana(iana: str) -> str:
    _, back = _tables()
    if iana in back:
        return back[iana]
    raise KeyError(iana)


def _state(zone: ZoneInfo, utc_ts: int) -> tuple[dt.timedelta, dt.timedelta, str]:
    local = dt.datetime.fromtimestamp(utc_ts, dt.timezone.utc).astimezone(zone)
    return local.utcoffset() or dt.timedelta(0), local.dst() or dt.timedelta(0), local.tzname() or ""


def _switch_point(zone: ZoneInfo, left: int, right: int) -> int:
    """First second from which the state differs from `left` (assumes left != right)."""
    outcome = _state(zone, left)
    while right - left > 1:
        middle = (left + right) // 2
        if _state(zone, middle) == outcome:
            left = middle
        else:
            right = middle
    return right


def _as_date(value: dt.date) -> dt.date:
    return value.date() if isinstance(value, dt.datetime) else value


def vtimezone(iana: str, since: dt.date, until: dt.date) -> icalendar.Timezone:
    """VTIMEZONE for `iana` covering the range start..until (with a margin of one day on each side). Raises KeyError for an unknown zone."""
    try:
        zone = ZoneInfo(iana)
    except (ZoneInfoNotFoundError, ValueError, OSError) as error:
        raise KeyError(iana) from error
    first, last = sorted((_as_date(since), _as_date(until)))
    start = int(dt.datetime.combine(first - dt.timedelta(days=1), dt.time(), dt.timezone.utc).timestamp())
    end = int(dt.datetime.combine(last + dt.timedelta(days=2), dt.time(), dt.timezone.utc).timestamp())

    stages: list[tuple[int, tuple[dt.timedelta, dt.timedelta, str], tuple[dt.timedelta, dt.timedelta, str]]] = []
    before = _state(zone, start)
    stages.append((start, before, before))
    t = start
    while t < end:
        next = min(t + STEP_S, end)
        old, new = _state(zone, t), _state(zone, next)
        if old != new:
            point = _switch_point(zone, t, next)
            stages.append((point, _state(zone, point - 1), _state(zone, point)))
        t = next

    tz = icalendar.Timezone()
    tz.add("TZID", iana)
    for point, old, new in stages:
        kind = icalendar.TimezoneDaylight() if new[1] else icalendar.TimezoneStandard()
        start_local = (dt.datetime.fromtimestamp(point, dt.timezone.utc) + old[0]).replace(tzinfo=None)
        kind.add("DTSTART", start_local)
        kind.add("TZOFFSETFROM", old[0])
        kind.add("TZOFFSETTO", new[0])
        if new[2]:
            kind.add("TZNAME", new[2])
        tz.add_component(kind)
    return tz
