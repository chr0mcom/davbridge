"""Series for reading, EAS 14.1: RRULE/EXDATE/RECURRENCE-ID -> `Calendar:Recurrence` and `Calendar:Exceptions`. Pure.

Principle: never deliver a wrong series. Anything that cannot be expressed losslessly in the EAS patterns yields `None` with a
reason (`is_mappable`), and the caller treats the item as invisible.

Mapped are FREQ=DAILY|WEEKLY|MONTHLY|YEARLY with INTERVAL, BYDAY, BYMONTHDAY, BYMONTH, COUNT and UNTIL. In addition there are
deletions (EXDATE) and overriding appointments (RECURRENCE-ID). The recurrence applies in the local time of the master's zone,
the way EAS evaluates it in the appointment's time zone (daylight saving time therefore stays correct).

Stricter than the protocol requires (intent: better hide than show wrongly, each with its own reason):
- the start itself must be the first instance of the rule (`start-does-not-match`);
- month days above 28 and February 29 are not mapped, because iCalendar skips such months and EAS does not;
- weekly with INTERVAL > 1 and several days including Sunday depends on the start of the week (`week-sunday`);
- yearly only with INTERVAL = 1; COUNT at most 999, INTERVAL at most 999; COUNT together with UNTIL not at all;
- exceptions with RANGE (THISANDFUTURE), without a matching instance, or without a reminder when the master has one.

Additions to the interface (additive only): `Series.master` (the master as `ical.Appointment`, default None) and the helper
values `compact`, `BUSY_STATUS`, `SENSITIVITY`, which `calendar.py` shares.
"""

from __future__ import annotations

import calendar
import datetime as dt
import itertools
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
from dateutil import rrule as rr

from .. import ical, timezones
from . import tags
from .wbxml import Node

UTC: Final = dt.timezone.utc
MAX_STEPS: Final = 5000
MAX_EXCEPTIONS: Final = 256
MAX_COUNTER: Final = 999
MAX_INTERVAL: Final = 999

BUSY_STATUS: Final[Mapping[str, int]] = MappingProxyType({"Free": 0, "Tentative": 1, "Busy": 2, "OOF": 3, "WorkingElsewhere": 2})  # 4 is forbidden in 14.1
SENSITIVITY: Final[Mapping[str, int]] = MappingProxyType({"PUBLIC": 0, "PRIVATE": 2, "CONFIDENTIAL": 3})

_DAYS: Final = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_WEEKDAY: Final = (rr.MO, rr.TU, rr.WE, rr.TH, rr.FR, rr.SA, rr.SU)
_BIT: Final = (2, 4, 8, 16, 32, 64, 1)
_BYDAY: Final = re.compile(r"^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$")
_SERIES_LINE: Final = re.compile(r"(?im)^(RRULE|RDATE|EXDATE|RECURRENCE-ID)[;:]")
_ALLOWED: Final = frozenset({"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "COUNT", "UNTIL", "WKST"})
_FREQ: Final = {"DAILY": rr.DAILY, "WEEKLY": rr.WEEKLY, "MONTHLY": rr.MONTHLY, "YEARLY": rr.YEARLY}


@dataclass(frozen=True, slots=True)
class Series:
    """Recurrence rule and exceptions as finished nodes of code page 4 (`Calendar:Recurrence`, one `Calendar:Exception` each)."""

    repetition: Node
    exceptions: tuple[Node, ...]
    master: ical.Appointment | None = None


class _NotMappable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def compact(value: dt.datetime) -> str:
    """Compact DateTime `yyyyMMddTHHmmssZ` ([MS-ASDTYPE] 2.7.2)."""
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _zone(name: str) -> dt.tzinfo:
    if name in timezones.UTC_NAMES:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise _NotMappable("zone") from None


def _local(value: Any, zone: dt.tzinfo, end_of_day: bool = False) -> dt.datetime:
    """Date or point in time -> local wall-clock time (naive) in `zone`. A plain date is midnight, as UNTIL (`end_of_day`) 23:59:59."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(microsecond=0)
        return value.astimezone(zone).replace(tzinfo=None, microsecond=0)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time(23, 59, 59) if end_of_day else dt.time())
    raise _NotMappable("time-value")


def _after_utc(local: dt.datetime, zone: dt.tzinfo) -> dt.datetime:
    return local.replace(tzinfo=zone).astimezone(UTC)


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _whole_number(value: Any, reason: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise _NotMappable(reason) from None
    if not minimum <= number <= maximum:
        raise _NotMappable(reason)
    return number


def _byday(values: list[Any]) -> list[tuple[int | None, int]]:
    """Weekday list -> (ordinal or None, weekday 0=Monday)."""
    result = []
    for raw in values:
        hit = _BYDAY.match(str(raw).upper())
        if hit is None:
            raise _NotMappable("byday")
        order = int(hit.group(1)) if hit.group(1) else None
        result.append((order, _DAYS.index(hit.group(2))))
    return result


def _node(name: str, value: object | None) -> Node | None:
    return tags.leaf(f"Calendar:{name}", value)


@dataclass(frozen=True, slots=True)
class _Pattern:
    type: int
    rule: dict[str, Any]
    day_mask: int | None = None
    month_day: int | None = None
    week: int | None = None
    month: int | None = None


def _single_order(entries: list[tuple[int | None, int]]) -> tuple[int, int]:
    if len(entries) != 1:
        raise _NotMappable("several-byday")
    order, tag = entries[0]
    if order is None:
        raise _NotMappable("byday-without-order")
    if order not in (1, 2, 3, 4, -1):
        raise _NotMappable("byday-order")
    return (5 if order == -1 else order), tag


def _month_day(parts: dict[str, list[Any]]) -> int | None:
    values = parts.get("BYMONTHDAY", [])
    if not values:
        return None
    if len(values) != 1:
        raise _NotMappable("several-month-days")
    tag = _whole_number(values[0], "month-day", -31, 31)
    if tag < 1:
        raise _NotMappable("negative-month-day")
    return tag


def _pattern(parts: dict[str, list[Any]], start: dt.datetime, interval: int) -> _Pattern:
    freq = str(parts["FREQ"][0]).upper()
    if freq not in _FREQ:
        raise _NotMappable(f"freq-{freq.lower()}"[:40])
    entries = _byday(parts.get("BYDAY", []))
    month_day = _month_day(parts)
    months = parts.get("BYMONTH", [])
    if len(months) > 1:
        raise _NotMappable("several-bymonth")
    month = _whole_number(months[0], "bymonth", 1, 12) if months else None
    base: dict[str, Any] = {"freq": _FREQ[freq], "dtstart": start, "interval": interval, "wkst": rr.MO}

    if freq == "DAILY":
        if entries or month_day is not None or month is not None:
            raise _NotMappable("daily-with-filter")
        return _Pattern(0, base)

    if freq == "WEEKLY":
        if month_day is not None or month is not None:
            raise _NotMappable("week-with-month")
        if any(order is not None for order, _ in entries):
            raise _NotMappable("week-order")
        days = sorted({tag for _, tag in entries}) or [start.weekday()]
        if interval > 1 and len(days) > 1 and 6 in days:
            raise _NotMappable("week-sunday")
        mask = sum(_BIT[t] for t in days)
        return _Pattern(1, {**base, "byweekday": [_WEEKDAY[t] for t in days]}, day_mask=mask)

    if freq == "MONTHLY":
        if month is not None:
            raise _NotMappable("month-with-bymonth")
        if entries:
            if month_day is not None:
                raise _NotMappable("month-byday-and-month-day")
            week, tag = _single_order(entries)
            order = -1 if week == 5 else week
            return _Pattern(3, {**base, "byweekday": _WEEKDAY[tag](order)}, day_mask=_BIT[tag], week=week)
        day_of_month = month_day if month_day is not None else start.day
        if day_of_month > 28:
            raise _NotMappable("month-day-over-28")
        return _Pattern(2, {**base, "bymonthday": day_of_month}, month_day=day_of_month)

    if interval != 1:
        raise _NotMappable("year-interval")
    if entries:
        if month_day is not None or month is None:
            raise _NotMappable("year-byday-without-month")
        week, tag = _single_order(entries)
        order = -1 if week == 5 else week
        return _Pattern(6, {**base, "bymonth": month, "byweekday": _WEEKDAY[tag](order)}, day_mask=_BIT[tag], week=week, month=month)
    if month_day is not None and month is None:
        raise _NotMappable("year-month-day-without-month")
    choice_month = month if month is not None else start.month
    choice_tag = month_day if month_day is not None else start.day
    if choice_tag > calendar.monthrange(2026, choice_month)[1]:
        raise _NotMappable("leap-day")
    return _Pattern(5, {**base, "bymonth": choice_month, "bymonthday": choice_tag}, month_day=choice_tag, month=choice_month)


def _all_exdates(master: icalendar.Event, zone: dt.tzinfo) -> set[dt.datetime]:
    result: set[dt.datetime] = set()
    for property in _list(master.get("EXDATE")):
        for entry in getattr(property, "dts", []):
            result.add(_local(entry.dt, zone))
    return result


def _exception_fields(t: ical.Appointment) -> list[Node | None]:
    """Fields of an overriding instance; location and subject always, so that nothing of the master shows through."""
    return [
        _node("AllDayEvent", 1 if t.all_day else 0),
        _node("BusyStatus", BUSY_STATUS.get(t.free_occupied, 2)),
        _node("DtStamp", compact(t.changed or t.created or t.start)),
        _node("EndTime", compact(t.end)),
        _node("Location", t.location),
        _node("Reminder", t.reminder_min),
        _node("Sensitivity", SENSITIVITY.get(t.classification, 0)),
        _node("Subject", t.subject),
        _node("StartTime", compact(t.start)),
    ]


def _build(vevents: list[icalendar.Event], appointments: list[ical.Appointment]) -> Series:
    if len(appointments) != len(vevents) or any(t.reason != "series" for t in appointments):
        raise _NotMappable("invalid")
    if any("RDATE" in ev for ev in vevents):
        raise _NotMappable("rdate")
    if len({str(ev.get("UID", "")) for ev in vevents}) != 1 or not str(vevents[0].get("UID", "")):
        raise _NotMappable("several-uid")
    master_number = [i for i, ev in enumerate(vevents) if "RECURRENCE-ID" not in ev]
    if not master_number:
        raise _NotMappable("exception-without-master")
    if len(master_number) > 1:
        raise _NotMappable("several-masters")
    master, m_appointment = vevents[master_number[0]], appointments[master_number[0]]
    raw = master.get("RRULE")
    if raw is None:
        raise _NotMappable("without-rrule")
    if isinstance(raw, list):
        raise _NotMappable("several-rrule")
    parts = {str(k).upper(): _list(v) for k, v in raw.items()}
    foreign = sorted(set(parts) - _ALLOWED)
    if foreign:
        raise _NotMappable(foreign[0].lower()[:40])
    if "FREQ" not in parts:
        raise _NotMappable("without-freq")
    zone = _zone(m_appointment.zone)
    start = _local(master.decoded("DTSTART"), zone)
    interval = _whole_number(parts["INTERVAL"][0], "interval", 1, MAX_INTERVAL) if parts.get("INTERVAL") else 1
    counter = _whole_number(parts["COUNT"][0], "count", 1, MAX_COUNTER) if parts.get("COUNT") else None
    end_raw = parts["UNTIL"][0] if parts.get("UNTIL") else None
    if counter is not None and end_raw is not None:
        raise _NotMappable("count-and-until")
    pattern = _pattern(parts, start, interval)
    end_local = _local(end_raw, zone, end_of_day=True) if end_raw is not None else None
    rule = {**pattern.rule}
    if counter is not None:
        rule["count"] = counter
    if end_local is not None:
        rule["until"] = end_local
    try:
        instances = list(itertools.islice(rr.rrule(**rule), MAX_STEPS + 1))
    except (ValueError, TypeError, OverflowError):
        raise _NotMappable("rule") from None
    if not instances:
        raise _NotMappable("until-before-start")
    if instances[0] != start:
        raise _NotMappable("start-does-not-match")
    complete = len(instances) <= MAX_STEPS
    instances = instances[:MAX_STEPS]
    known = set(instances)

    def is_instance(local: dt.datetime) -> bool | None:
        """True/False if certain; None if the expansion stops early and the point in time lies beyond it."""
        if local in known:
            return True
        if complete or local <= instances[-1]:
            return False
        return None

    exceptions: dict[dt.datetime, Node] = {}
    for local in sorted(_all_exdates(master, zone)):
        if is_instance(local) is False:
            continue
        exceptions[local] = tags.node("Calendar:Exception", _node("Deleted", 1), _node("ExceptionStartTime", compact(_after_utc(local, zone))))
    deleted = set(exceptions)
    seen: set[dt.datetime] = set()
    for number, ev in enumerate(vevents):
        if "RECURRENCE-ID" not in ev:
            continue
        if str(getattr(ev["RECURRENCE-ID"], "params", {}).get("RANGE", "")):
            raise _NotMappable("range")
        local = _local(ev.decoded("RECURRENCE-ID"), zone)
        if local in seen:
            raise _NotMappable("duplicate-exception")
        seen.add(local)
        if local in deleted:
            continue
        if is_instance(local) is False:
            raise _NotMappable("exception-without-instance")
        t = appointments[number]
        if m_appointment.reminder_min is not None and t.reminder_min is None:
            raise _NotMappable("exception-without-reminder")
        exceptions[local] = tags.node("Calendar:Exception", _node("Deleted", 0), _node("ExceptionStartTime", compact(_after_utc(local, zone))), *_exception_fields(t))
    if len(exceptions) > MAX_EXCEPTIONS:
        raise _NotMappable("too-many-exceptions")

    until: str | None = None
    if end_local is not None:
        last = instances[-1] if complete else end_local
        until = compact(_after_utc(last, zone))
    repetition = tags.node(
        "Calendar:Recurrence",
        _node("Type", pattern.type),
        _node("Interval", interval),
        _node("DayOfWeek", pattern.day_mask),
        _node("DayOfMonth", pattern.month_day),
        _node("WeekOfMonth", pattern.week),
        _node("MonthOfYear", pattern.month),
        _node("Occurrences", counter),
        _node("Until", until),
    )
    return Series(repetition, tuple(exceptions[k] for k in sorted(exceptions)), m_appointment)


def _analyze(ics: str, default_timezone: str) -> tuple[Series | None, str | None, bool]:
    """(series or None, reason, is_series). `is_series=False`: no series property at all in the object; unreadable counts as not mappable."""
    try:
        vevents = icalendar.Calendar.from_ical(ics).walk("VEVENT")
    except Exception:
        return None, "invalid", True
    if any(name in ical.SERIES_PROPERTIES for ev in vevents for name, _ in ev.errors):
        return None, "invalid", True
    if not any(p in ev for ev in vevents for p in ical.SERIES_PROPERTIES):
        return None, None, False
    try:
        return _build(vevents, ical.read(ics, default_timezone)), None, True
    except _NotMappable as error:
        return None, error.reason, True
    except Exception:
        return None, "invalid", True


def series_line_unreadable(ics: str) -> bool:
    """True if a VEVENT carries a series property that `icalendar` could not read and silently drops.

    `ical.read` would take such an object for a single appointment. Cheap pre-check by text pattern, parsing only on a hit."""
    if not _SERIES_LINE.search(ics):
        return False
    return any(name in ical.SERIES_PROPERTIES for ev in _vevents(ics) for name, _ in ev.errors)


def _vevents(ics: str) -> list[icalendar.Event]:
    try:
        return icalendar.Calendar.from_ical(ics).walk("VEVENT")
    except Exception:
        return []


def read_series(ics: str, default_timezone: str | None = None) -> Series | None:
    """Series of the object; `None` if it is not a series or cannot be mapped safely (reason: `is_mappable`)."""
    return _analyze(ics, default_timezone)[0]


def is_mappable(ics: str, default_timezone: str | None = None) -> tuple[bool, str | None]:
    """`(True, None)` for a single appointment or a mappable series, otherwise `(False, reason)` for the log (`eas_series_skipped`)."""
    series, reason, is_series = _analyze(ics, default_timezone)
    if not is_series:
        return True, None
    return (True, None) if series is not None else (False, reason)
