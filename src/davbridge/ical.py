"""EWS <-> iCalendar. One field table for all directions, read view, lossless patch, creation.

Pure: no network, no asyncio. Closed world when writing (all or nothing), open world when reading.
Feature sets are described in docs/architecture.md; series, attendees and attachments are only readable or rejected in every set.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
from lxml import etree
from lxml import html as lxml_html

from . import timezones
from .config import FeatureSet, runtime_default_zone
from .permissions import effective_rights

T_NS: Final = "http://schemas.microsoft.com/exchange/services/2006/types"
PRODID: Final = "-//DAVbridge contributors//DAVbridge//EN"
UTC: Final = dt.timezone.utc
FILE_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@%+-]{0,199}\.ics$")
SERIES_PROPERTIES: Final = ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID")
_FIXED: Final = dt.datetime(2000, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextZone:
    """Zone in which naive EWS times are read and new times are written. `all_day` decides the form of start/end."""

    iana: str = field(default_factory=runtime_default_zone)
    end_iana: str | None = None
    all_day: bool | None = None

    def for_end(self) -> str:
        return self.end_iana or self.iana


@dataclass(frozen=True, slots=True)
class Appointment:
    """Read view, RAM only. `representable=False` means: never show, never touch."""

    uid: str
    subject: str
    location: str
    text: str
    html: str | None
    start: dt.datetime
    end: dt.datetime
    all_day: bool
    zone: str
    free_occupied: Literal["Free", "Tentative", "Busy", "OOF", "WorkingElsewhere"]
    created: dt.datetime | None
    changed: dt.datetime | None
    sequence: int
    has_organizer: bool
    has_attendee: bool
    attendee_display: tuple[str, ...]
    classification: str
    status: str
    representable: bool
    reason: str | None
    reminder_min: int | None = None
    categories: tuple[str, ...] = ()
    organizer: tuple[str, str] | None = None


class FieldClass(StrEnum):
    WRITE = "write"
    DEFAULT_TOLERATED = "default"
    READ_ONLY = "read"


@dataclass(frozen=True, slots=True)
class Field:
    field_uri: str
    classification: FieldClass
    min_feature_set: FeatureSet
    from_ews: Callable[[etree._Element, ContextZone], Any]
    in_ics: Callable[[icalendar.Event, Any, ContextZone], None]
    from_ics: Callable[[icalendar.Event], Any]
    as_ews: Callable[[Appointment], etree._Element | None]
    neutral: Any = None
    element: str = ""


@dataclass(frozen=True, slots=True)
class FieldChange:
    field_uri: str
    kind: Literal["set", "delete"]
    value: Any


class FieldRejected(Exception):
    def __init__(self, field_uri: str, reason: Literal["unknown-field", "field-disabled", "read-only", "attendees", "series", "attachments"]) -> None:
        super().__init__(f"{field_uri}: {reason}")
        self.field_uri = field_uri
        self.reason = reason


class PermissionsLike(Protocol):
    read: bool
    change: bool
    delete: bool
    create: bool
    is_organizer: bool


# ---------------------------------------------------------------------------------------------------------------------
# Helpers: XML
# ---------------------------------------------------------------------------------------------------------------------

_INVALID_XML: Final = re.compile("[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _clean(text: str) -> str:
    """Removes characters forbidden in XML 1.0 from foreign texts; lxml would raise ValueError otherwise."""
    return _INVALID_XML.sub("", text)


def _t(name: str, text: str | None = None, **attrib: str) -> etree._Element:
    element = etree.Element(f"{{{T_NS}}}{name}", nsmap={"t": T_NS})
    for key, value in attrib.items():
        element.set(key, _clean(value))
    if text is not None:
        element.text = _clean(text)
    return element


def _local(element: etree._Element) -> str:
    return etree.QName(element).localname if isinstance(element.tag, str) else ""


def _children(element: etree._Element) -> list[etree._Element]:
    return [k for k in element if isinstance(k.tag, str)]


def _text(element: etree._Element) -> str:
    return (element.text or "").strip()


def _time_text(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bool(text: str) -> bool:
    value = text.strip().lower()
    if value in {"true", "1"}:
        return True
    if value in {"false", "0"}:
        return False
    raise ValueError("boolean expected")


def _iso(text: str, zone: str) -> dt.datetime:
    """xs:dateTime → tz-aware UTC. Naive Werte gelten in `zone`."""
    raw = text.strip()
    if not raw:
        raise ValueError("time is missing")
    value = dt.datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=_zone(zone))
    return value.astimezone(UTC).replace(microsecond=0)


def _zone(iana: str) -> dt.tzinfo:
    if iana in timezones.UTC_NAMES:
        return UTC
    try:
        return ZoneInfo(iana)
    except (ZoneInfoNotFoundError, ValueError, OSError) as error:
        raise KeyError(iana) from error


# ---------------------------------------------------------------------------------------------------------------------
# Reading: iCalendar -> appointment
# ---------------------------------------------------------------------------------------------------------------------


def _zone_name(info: dt.tzinfo | None, default: str) -> str:
    key = getattr(info, "key", None)
    if info is UTC or key in timezones.UTC_NAMES:
        return "UTC"
    if isinstance(key, str) and key:
        return key
    return default


def _time_value(ev: icalendar.Event, name: str, default: str) -> tuple[dt.datetime, bool, str]:
    raw = ev.decoded(name)
    if isinstance(raw, dt.datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=_zone(default)).astimezone(UTC), False, default
        return raw.astimezone(UTC), False, _zone_name(raw.tzinfo, default)
    if isinstance(raw, dt.date):
        midnight = dt.datetime.combine(raw, dt.time(), _zone(default))
        return midnight.astimezone(UTC), True, default
    raise ValueError(name)


def _plus_tag(value: dt.datetime, zone: str) -> dt.datetime:
    local = value.astimezone(_zone(zone))
    return dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time(), _zone(zone)).astimezone(UTC)


def _combo_time(value: Any, default: str) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return (value if value.tzinfo else value.replace(tzinfo=_zone(default))).astimezone(UTC)
    return None


def _first_display_reminder(ev: icalendar.Event) -> icalendar.Alarm | None:
    for alarm in ev.walk("VALARM"):
        if str(alarm.get("ACTION", "")).upper() == "DISPLAY":
            return alarm
    return None


def _reminder_min(ev: icalendar.Event) -> int | None:
    alarm = _first_display_reminder(ev)
    if alarm is None or "TRIGGER" not in alarm:
        return None
    try:
        gap = alarm.decoded("TRIGGER")
    except Exception:
        return None
    if isinstance(gap, dt.timedelta) and gap <= dt.timedelta(0):
        return int(-gap.total_seconds() // 60)
    return None


def _free_occupied(ev: icalendar.Event) -> str:
    if str(ev.get("TRANSP", "")).upper() == "TRANSPARENT":
        return "Free"
    mapping = {"FREE": "Free", "TENTATIVE": "Tentative", "BUSY": "Busy", "OOF": "OOF", "WORKINGELSEWHERE": "WorkingElsewhere"}
    set = mapping.get(str(ev.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).upper())
    if set:
        return set
    return "Tentative" if str(ev.get("STATUS", "")).upper() == "TENTATIVE" else "Busy"


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _attendee(ev: icalendar.Event) -> tuple[str, ...]:
    result = []
    for entry in _list(ev.get("ATTENDEE")):
        name = str(getattr(entry, "params", {}).get("CN", "")).strip()
        address = re.sub(r"(?i)^mailto:", "", str(entry)).strip()
        result.append(name or address)
    return tuple(result)


def _organizer(ev: icalendar.Event) -> tuple[str, str] | None:
    org = ev.get("ORGANIZER")
    if org is None:
        return None
    org = _list(org)[0]
    address = re.sub(r"(?i)^mailto:", "", str(org)).strip()
    name = str(getattr(org, "params", {}).get("CN", "")).strip() or address
    return name, address


def _categories(ev: icalendar.Event) -> tuple[str, ...]:
    result: list[str] = []
    for entry in _list(ev.get("CATEGORIES")):
        result.extend(str(c) for c in getattr(entry, "cats", []))
    return tuple(result)


def _html(ev: icalendar.Event) -> str | None:
    for entry in _list(ev.get("X-ALT-DESC")):
        kind = str(getattr(entry, "params", {}).get("FMTTYPE", "")).lower()
        if kind in ("", "text/html"):
            return str(entry)
    return None


def _appointment(ev: icalendar.Event, default: str, reason: str | None) -> Appointment:
    start, all_day, zone = _time_value(ev, "DTSTART", default)
    if "DTEND" in ev:
        end = _time_value(ev, "DTEND", default)[0]
    elif "DURATION" in ev:
        end = start + ev.decoded("DURATION")
    else:
        end = _plus_tag(start, default if zone == "UTC" else zone) if all_day else start
    if end < start:
        end = start
    organizer = _organizer(ev)
    attendee = _attendee(ev)
    return Appointment(
        uid=str(ev.get("UID", "")),
        subject=str(ev.get("SUMMARY", "")),
        location=str(ev.get("LOCATION", "")),
        text=str(ev.get("DESCRIPTION", "")),
        html=_html(ev),
        start=start,
        end=end,
        all_day=all_day,
        zone=zone,
        free_occupied=_free_occupied(ev),
        created=_combo_time(ev.decoded("CREATED") if "CREATED" in ev else None, default),
        changed=_combo_time(ev.decoded("LAST-MODIFIED") if "LAST-MODIFIED" in ev else None, default),
        sequence=int(str(ev.get("SEQUENCE", "0")) or 0),
        has_organizer=organizer is not None,
        has_attendee=bool(attendee),
        attendee_display=attendee,
        classification=str(ev.get("CLASS", "PUBLIC")).upper() or "PUBLIC",
        status=str(ev.get("STATUS", "CONFIRMED")).upper() or "CONFIRMED",
        representable=reason is None,
        reason=reason,
        reminder_min=_reminder_min(ev),
        categories=_categories(ev),
        organizer=organizer,
    )


def _placeholder(reason: str, uid: str = "") -> Appointment:
    empty = dt.datetime(1970, 1, 1, tzinfo=UTC)
    return Appointment(uid, "", "", "", None, empty, empty, False, "UTC", "Busy", None, None, 0, False, False, (), "PUBLIC", "CONFIRMED", False, reason)


def read(ics: str, default_timezone: str | None = None) -> list[Appointment]:
    """One appointment per VEVENT. Anything not representable (series, several VEVENTs, VTODO, broken) carries `representable=False` and a reason."""
    default_timezone = default_timezone or runtime_default_zone()
    try:
        calendar = icalendar.Calendar.from_ical(ics)
        vevents = calendar.walk("VEVENT")
    except Exception:
        return [_placeholder("invalid")]
    if not vevents:
        return [_placeholder("vtodo" if calendar.walk("VTODO") else "no-vevent")]
    series = any(p in ev for ev in vevents for p in SERIES_PROPERTIES)
    reason = "series" if series else ("several-vevent" if len(vevents) > 1 else None)
    result = []
    for ev in vevents:
        try:
            result.append(_appointment(ev, default_timezone, reason))
        except Exception:
            result.append(_placeholder("invalid", str(ev.get("UID", ""))))
    return result


def representable(ics: str) -> tuple[bool, str | None]:
    """The single visibility filter. No series is representable."""
    appointments = read(ics)
    if len(appointments) == 1:
        return appointments[0].representable, appointments[0].reason
    return False, appointments[0].reason or "several-vevent"


# ---------------------------------------------------------------------------------------------------------------------
# Writing: building blocks
# ---------------------------------------------------------------------------------------------------------------------


def _set_nested(ev: icalendar.Component, name: str, value: Any, **parameter: str) -> None:
    ev.pop(name, None)
    ev.add(name, value, parameters=parameter or None)


def _is_date(ev: icalendar.Event, name: str) -> bool | None:
    if name not in ev:
        return None
    raw = ev.decoded(name)
    return not isinstance(raw, dt.datetime) and isinstance(raw, dt.date)


def _write_time(ev: icalendar.Event, name: str, instant: dt.datetime, zone: str, all_day: bool) -> None:
    if all_day:
        _set_nested(ev, name, instant.astimezone(_zone(zone)).date())
    else:
        _set_nested(ev, name, instant.astimezone(_zone(zone)))


def _html_to_text(html: str) -> str:
    prepared = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", html)
    try:
        text = lxml_html.fromstring(f"<div>{prepared}</div>").text_content()
    except Exception:
        text = re.sub(r"<[^>]+>", "", prepared)
    lines = [z.rstrip() for z in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip("\n")


def _normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _local_time_change(ev: icalendar.Event, flag: ContextZone, after_all_day: bool) -> None:
    """Switch the time form of start and end between date and date+time (IsAllDayEvent)."""
    for name, zone in (("DTSTART", flag.iana), ("DTEND", flag.for_end())):
        if name not in ev:
            continue
        is_date = bool(_is_date(ev, name))
        raw = ev.decoded(name)
        if after_all_day and not is_date:
            _set_nested(ev, name, (raw.astimezone(_zone(zone)) if raw.tzinfo else raw).date())
        elif not after_all_day and is_date:
            _set_nested(ev, name, dt.datetime.combine(raw, dt.time(), _zone(zone)))
    if after_all_day and "DTSTART" in ev:
        start = ev.decoded("DTSTART")
        end = ev.decoded("DTEND") if "DTEND" in ev else None
        if end is None or end <= start:
            _set_nested(ev, "DTEND", start + dt.timedelta(days=1))
    if "DURATION" in ev and ("DTSTART" in ev):
        ev.pop("DURATION", None)


# --- field functions ---------------------------------------------------------------------------------------------------


def _from_text(element: etree._Element, _flag: ContextZone) -> str:
    return _normalize_text(element.text or "")


def _in_summary(ev: icalendar.Event, value: Any, _flag: ContextZone) -> None:
    ev.pop("SUMMARY", None)
    if value is not None:
        ev.add("SUMMARY", value)


def _in_location(ev: icalendar.Event, value: Any, _flag: ContextZone) -> None:
    ev.pop("LOCATION", None)
    if value:
        ev.add("LOCATION", value)


def _from_body(element: etree._Element, _flag: ContextZone) -> tuple[str, str]:
    kind = (element.get("BodyType") or "Text").strip().lower()
    if kind not in {"text", "html"}:
        raise ValueError("BodyType")
    return kind, _normalize_text(element.text or "")


def _in_body(ev: icalendar.Event, value: Any, _flag: ContextZone) -> None:
    ev.pop("DESCRIPTION", None)
    ev.pop("X-ALT-DESC", None)
    if value is None:
        return
    kind, content = value
    if kind == "html":
        ev.add("X-ALT-DESC", content, parameters={"FMTTYPE": "text/html"})
        ev.add("DESCRIPTION", _html_to_text(content))
    elif content:
        ev.add("DESCRIPTION", content)


def _from_body_ics(ev: icalendar.Event) -> tuple[str, str]:
    html = _html(ev)
    return ("html", html) if html else ("text", str(ev.get("DESCRIPTION", "")))


def _as_body(t: Appointment) -> etree._Element | None:
    if t.html:
        return _t("Body", t.html, BodyType="HTML")
    return _t("Body", t.text, BodyType="Text")


def _from_time(element: etree._Element, flag: ContextZone) -> dt.datetime:
    return _iso(element.text or "", flag.iana)


def _from_time_end(element: etree._Element, flag: ContextZone) -> dt.datetime:
    return _iso(element.text or "", flag.for_end())


def _all_day_for(ev: icalendar.Event, name: str, flag: ContextZone) -> bool:
    if flag.all_day is not None:
        return flag.all_day
    return bool(_is_date(ev, name))


def _in_start(ev: icalendar.Event, value: Any, flag: ContextZone) -> None:
    if value is None:
        raise ValueError("start is required")
    _write_time(ev, "DTSTART", value, flag.iana, _all_day_for(ev, "DTSTART", flag))


def _in_end(ev: icalendar.Event, value: Any, flag: ContextZone) -> None:
    if value is None:
        raise ValueError("end is required")
    ev.pop("DURATION", None)
    all_day = _all_day_for(ev, "DTSTART", flag) if "DTSTART" in ev else bool(flag.all_day)
    _write_time(ev, "DTEND", value, flag.for_end(), all_day)


def _from_start_ics(ev: icalendar.Event) -> dt.datetime:
    return _time_value(ev, "DTSTART", runtime_default_zone())[0]


def _from_end_ics(ev: icalendar.Event) -> dt.datetime:
    if "DTEND" in ev:
        return _time_value(ev, "DTEND", runtime_default_zone())[0]
    return _appointment(ev, runtime_default_zone(), None).end


def _from_all_day(element: etree._Element, _flag: ContextZone) -> bool:
    return _bool(element.text or "")


def _in_all_day(ev: icalendar.Event, value: Any, flag: ContextZone) -> None:
    if value is None:
        raise ValueError("IsAllDayEvent is required")
    if "DTSTART" in ev:
        _local_time_change(ev, flag, bool(value))


def _from_all_day_ics(ev: icalendar.Event) -> bool:
    return bool(_is_date(ev, "DTSTART"))


def _as_time(t: Appointment, name: str) -> etree._Element | None:
    return _t(name, _time_text(t.start if name == "Start" else t.end))


def _zones_id(t: Appointment) -> str:
    try:
        return timezones.windows_for_iana(t.zone)
    except KeyError:
        return "UTC"


def _from_zone_id(element: etree._Element, _flag: ContextZone) -> str:
    identifier = element.get("Id") or element.get("TimeZoneName") or ""
    if not identifier:
        raise ValueError("zone id is missing")
    return timezones.iana_for_windows(identifier)


def _no_effect(_ev: icalendar.Event, _value: Any, _flag: ContextZone) -> None:
    return None


def _from_zone_ics(ev: icalendar.Event) -> str:
    return _time_value(ev, "DTSTART", runtime_default_zone())[2]


def _as_zone(name: str) -> Callable[[Appointment], etree._Element | None]:
    def build(t: Appointment) -> etree._Element | None:
        identifier = _zones_id(t)
        return _t(name, Id=identifier, Name=identifier)

    return build


_FREE_OCCUPIED: Final = {
    "Free": ("TRANSPARENT", "FREE"),
    "Tentative": ("OPAQUE", "TENTATIVE"),
    "Busy": ("OPAQUE", "BUSY"),
    "OOF": ("OPAQUE", "OOF"),
    "WorkingElsewhere": ("OPAQUE", "WORKINGELSEWHERE"),
}


def _from_free_occupied(element: etree._Element, _flag: ContextZone) -> str:
    value = _text(element)
    if value not in _FREE_OCCUPIED:
        raise ValueError("LegacyFreeBusyStatus")
    return value


def _in_free_occupied(ev: icalendar.Event, value: Any, _flag: ContextZone) -> None:
    ev.pop("TRANSP", None)
    ev.pop("X-MICROSOFT-CDO-BUSYSTATUS", None)
    if value is not None:
        transp, status = _FREE_OCCUPIED[value]
        ev.add("TRANSP", transp)
        ev.add("X-MICROSOFT-CDO-BUSYSTATUS", status)


_SENSITIVITY: Final = {"Normal": None, "Personal": "PRIVATE", "Private": "PRIVATE", "Confidential": "CONFIDENTIAL"}


def _from_sensitivity(element: etree._Element, _flag: ContextZone) -> str:
    value = _text(element)
    if value not in _SENSITIVITY:
        raise ValueError("Sensitivity")
    return value


def _in_sensitivity(ev: icalendar.Event, value: Any, _flag: ContextZone) -> None:
    ev.pop("CLASS", None)
    classification = _SENSITIVITY[value] if value is not None else None
    if classification:
        ev.add("CLASS", classification)


def _from_sensitivity_ics(ev: icalendar.Event) -> str:
    return {"PRIVATE": "Private", "CONFIDENTIAL": "Confidential"}.get(str(ev.get("CLASS", "")).upper(), "Normal")


def _as_sensitivity(t: Appointment) -> etree._Element | None:
    return _t("Sensitivity", {"PRIVATE": "Private", "CONFIDENTIAL": "Confidential"}.get(t.classification, "Normal"))


def _from_categories(element: etree._Element, _flag: ContextZone) -> tuple[str, ...]:
    names = [_text(k) for k in _children(element) if _local(k) == "String"]
    return tuple(n for n in names if n)


def _in_categories(ev: icalendar.Event, value: Any, _flag: ContextZone) -> None:
    ev.pop("CATEGORIES", None)
    if value:
        ev.add("CATEGORIES", list(value))


def _as_categories(t: Appointment) -> etree._Element | None:
    if not t.categories:
        return None
    root = _t("Categories")
    for name in t.categories:
        etree.SubElement(root, f"{{{T_NS}}}String").text = _clean(name)
    return root


def _from_bool(element: etree._Element, _flag: ContextZone) -> bool:
    return _bool(element.text or "")


def _from_int(element: etree._Element, _flag: ContextZone) -> int:
    value = int(_text(element))
    if value < 0 or value > 40320 * 10:
        raise ValueError("minutes out of range")
    return value


def _in_reminder_set(ev: icalendar.Event, value: Any, _flag: ContextZone) -> None:
    alarm = _first_display_reminder(ev)
    if value is True and alarm is None:
        new = icalendar.Alarm()
        new.add("ACTION", "DISPLAY")
        new.add("DESCRIPTION", str(ev.get("SUMMARY", "Appointment")) or "Appointment")
        new.add("TRIGGER", dt.timedelta(minutes=-15))
        ev.add_component(new)
    elif value in (False, None) and alarm is not None:
        ev.subcomponents.remove(alarm)


def _in_reminder_minutes(ev: icalendar.Event, value: Any, _flag: ContextZone) -> None:
    alarm = _first_display_reminder(ev)
    if value is None:
        if alarm is not None:
            ev.subcomponents.remove(alarm)
        return
    if alarm is None:
        alarm = icalendar.Alarm()
        alarm.add("ACTION", "DISPLAY")
        alarm.add("DESCRIPTION", str(ev.get("SUMMARY", "Appointment")) or "Appointment")
        ev.add_component(alarm)
    alarm.pop("TRIGGER", None)
    alarm.add("TRIGGER", dt.timedelta(minutes=-int(value)))


def _from_reminder_set_ics(ev: icalendar.Event) -> bool:
    return _reminder_min(ev) is not None


def _as_reminder_set(t: Appointment) -> etree._Element | None:
    return _t("ReminderIsSet", "true" if t.reminder_min is not None else "false")


def _as_reminder_minutes(t: Appointment) -> etree._Element | None:
    return None if t.reminder_min is None else _t("ReminderMinutesBeforeStart", str(t.reminder_min))


def _from_enum(allowed: frozenset[str]) -> Callable[[etree._Element, ContextZone], str]:
    def read(element: etree._Element, _flag: ContextZone) -> str:
        value = _text(element)
        if value not in allowed:
            raise ValueError("enumeration value")
        return value

    return read


def _constant(name: str, value: str) -> Callable[[Appointment], etree._Element | None]:
    return lambda _t_: _t(name, value)


def _read_uid(t: Appointment) -> etree._Element | None:
    return _t("UID", t.uid)


def _field(
    uri: str, classification: FieldClass, min_feature_set: FeatureSet, from_ews: Callable, in_ics: Callable, from_ics: Callable, as_ews: Callable, element: str, neutral: Any = None
) -> tuple[str, Field]:
    return uri, Field(uri, classification, min_feature_set, from_ews, in_ics, from_ics, as_ews, neutral, element)


def _read_only(uri: str, element: str, as_ews: Callable[[Appointment], etree._Element | None] | None = None) -> tuple[str, Field]:
    return _field(uri, FieldClass.READ_ONLY, FeatureSet.BASIC, _from_text, _no_effect, lambda ev: None, as_ews or (lambda t: None), element)


def _table() -> Mapping[str, Field]:
    sch, tol, basic, default = FieldClass.WRITE, FieldClass.DEFAULT_TOLERATED, FeatureSet.BASIC, FeatureSet.DEFAULT
    entries = [
        _field("item:Subject", sch, basic, _from_text, _in_summary, lambda ev: str(ev.get("SUMMARY", "")), lambda t: _t("Subject", t.subject), "Subject"),
        _field("item:Body", sch, basic, _from_body, _in_body, _from_body_ics, _as_body, "Body"),
        _field("calendar:Location", sch, basic, _from_text, _in_location, lambda ev: str(ev.get("LOCATION", "")), lambda t: _t("Location", t.location), "Location"),
        _field("calendar:Start", sch, basic, _from_time, _in_start, _from_start_ics, lambda t: _as_time(t, "Start"), "Start"),
        _field("calendar:End", sch, basic, _from_time_end, _in_end, _from_end_ics, lambda t: _as_time(t, "End"), "End"),
        _field("calendar:IsAllDayEvent", sch, basic, _from_all_day, _in_all_day, _from_all_day_ics, lambda t: _t("IsAllDayEvent", "true" if t.all_day else "false"), "IsAllDayEvent"),
        _field("calendar:StartTimeZone", sch, basic, _from_zone_id, _no_effect, _from_zone_ics, _as_zone("StartTimeZone"), "StartTimeZone"),
        _field("calendar:EndTimeZone", sch, basic, _from_zone_id, _no_effect, _from_zone_ics, _as_zone("EndTimeZone"), "EndTimeZone"),
        _field("calendar:MeetingTimeZone", sch, basic, _from_zone_id, _no_effect, _from_zone_ics, lambda t: None, "MeetingTimeZone"),
        _field("calendar:LegacyFreeBusyStatus", sch, basic, _from_free_occupied, _in_free_occupied, _free_occupied, lambda t: _t("LegacyFreeBusyStatus", t.free_occupied), "LegacyFreeBusyStatus"),
        _field("item:Sensitivity", sch, default, _from_sensitivity, _in_sensitivity, _from_sensitivity_ics, _as_sensitivity, "Sensitivity", neutral="Normal"),
        _field("item:Categories", sch, default, _from_categories, _in_categories, _categories, _as_categories, "Categories", neutral=()),
        _field("item:ReminderIsSet", sch, basic, _from_bool, _in_reminder_set, _from_reminder_set_ics, _as_reminder_set, "ReminderIsSet", neutral=False),
        _field("item:ReminderMinutesBeforeStart", sch, basic, _from_int, _in_reminder_minutes, _reminder_min, _as_reminder_minutes, "ReminderMinutesBeforeStart"),
        _field("item:Importance", tol, basic, _from_enum(frozenset({"Low", "Normal", "High"})), _no_effect, lambda ev: "Normal", _constant("Importance", "Normal"), "Importance", neutral="Normal"),
        _field("item:ItemClass", tol, basic, _from_text, _no_effect, lambda ev: "IPM.Appointment", _constant("ItemClass", "IPM.Appointment"), "ItemClass", neutral="IPM.Appointment"),
        _field(
            "calendar:IsResponseRequested", tol, basic, _from_bool, _no_effect, lambda ev: False, _constant("IsResponseRequested", "false"), "IsResponseRequested", neutral=frozenset({True, False})
        ),
        _field("calendar:CalendarItemType", tol, basic, _from_text, _no_effect, lambda ev: "Single", _constant("CalendarItemType", "Single"), "CalendarItemType", neutral="Single"),
        _field("calendar:AllowNewTimeProposal", tol, basic, _from_bool, _no_effect, lambda ev: True, lambda t: None, "AllowNewTimeProposal", neutral=frozenset({True, False})),
        _field("calendar:EnhancedLocation", tol, basic, lambda el, flag: None, _no_effect, lambda ev: None, lambda t: None, "EnhancedLocation", neutral=frozenset({None})),
        _read_only("calendar:UID", "UID", _read_uid),
        _read_only("item:DateTimeCreated", "DateTimeCreated"),
        _read_only("item:LastModifiedTime", "LastModifiedTime"),
        _read_only("calendar:IsMeeting", "IsMeeting"),
        _read_only("calendar:IsCancelled", "IsCancelled"),
        _read_only("calendar:IsRecurring", "IsRecurring"),
        _read_only("calendar:IsOrganizer", "IsOrganizer"),
        _read_only("calendar:Organizer", "Organizer"),
        _read_only("calendar:AppointmentSequenceNumber", "AppointmentSequenceNumber"),
        _read_only("calendar:MyResponseType", "MyResponseType"),
    ]
    return MappingProxyType(dict(entries))


FIELDS: Final[Mapping[str, Field]] = _table()
ELEMENT_TO_URI: Final[Mapping[str, str]] = MappingProxyType({f.element: uri for uri, f in FIELDS.items() if f.element})

_ATTENDEE: Final = frozenset({"RequiredAttendees", "OptionalAttendees", "Resources", "Organizer", "ConflictingMeetings", "AdjacentMeetings", "DuplicateMeetings"})
_SERIES: Final = frozenset({"Recurrence", "FirstOccurrence", "LastOccurrence", "ModifiedOccurrences", "DeletedOccurrences", "OriginalStart", "RecurrenceId"})
_ATTACHMENT: Final = frozenset({"Attachments", "MeetingRequestWasSent", "ConferenceType", "IsOnlineMeeting", "OnlineMeetingSettings"})
_ZONES_URIS: Final = frozenset({"calendar:StartTimeZone", "calendar:EndTimeZone", "calendar:MeetingTimeZone"})
_TIME_URIS: Final = frozenset({"calendar:Start", "calendar:End", "calendar:IsAllDayEvent"} | _ZONES_URIS)
_ORDER: Final = {"calendar:IsAllDayEvent": 0, "calendar:Start": 1, "calendar:End": 2}


def _reason_unknown(name: str) -> Literal["unknown-field", "attendees", "series", "attachments"]:
    short = name.split(":", 1)[-1]
    if short in _ATTENDEE:
        return "attendees"
    if short in _SERIES:
        return "series"
    if short in _ATTACHMENT:
        return "attachments"
    return "unknown-field"


def _is_neutral(value: Any, neutral: Any) -> bool:
    if neutral is None:
        return False
    if isinstance(neutral, frozenset):
        return value in neutral
    if isinstance(neutral, tuple):
        return tuple(value) == neutral
    return value == neutral


# ---------------------------------------------------------------------------------------------------------------------
# Writing: read changes, patch, creation
# ---------------------------------------------------------------------------------------------------------------------


def _flag_from(base: ContextZone, changes: Sequence[FieldChange], ev: icalendar.Event | None = None) -> ContextZone:
    """Zones: change > existing TZID of the appointment > default. All-day only if IsAllDayEvent was sent along."""
    after = {a.field_uri: a.value for a in changes if a.kind == "set"}
    start = after.get("calendar:StartTimeZone") or after.get("calendar:MeetingTimeZone")
    end = after.get("calendar:EndTimeZone")
    if ev is not None:
        start = start or _existing_zone(ev, "DTSTART")
        end = end or _existing_zone(ev, "DTEND")
    return ContextZone(iana=start or base.iana, end_iana=end or start or base.end_iana, all_day=after.get("calendar:IsAllDayEvent"))


def _existing_zone(ev: icalendar.Event, name: str) -> str | None:
    if name not in ev:
        return None
    identifier = str(getattr(ev[name], "params", {}).get("TZID", ""))
    if not identifier:
        value = ev.decoded(name)
        return "UTC" if isinstance(value, dt.datetime) and value.tzinfo is not None and _zone_name(value.tzinfo, "") == "UTC" else None
    try:
        return timezones.iana_for_windows(identifier)
    except KeyError:
        return None


def _process(entries: Sequence[tuple[str, str, etree._Element | None]], features: FeatureSet, zone: ContextZone) -> list[FieldChange]:
    """Checks every field against the table and the feature set (all or nothing) and converts the values. Raises FieldRejected or ValueError/KeyError."""
    fields: list[tuple[str, str, Field, etree._Element | None]] = []
    for uri, kind, element in entries:
        field = FIELDS.get(uri)
        if field is None:
            raise FieldRejected(uri, _reason_unknown(uri))
        if field.classification is FieldClass.READ_ONLY:
            raise FieldRejected(uri, "read-only")
        fields.append((uri, kind, field, element))

    zones: list[FieldChange] = []
    for uri, kind, field, element in fields:
        if uri in _ZONES_URIS:
            if kind == "delete" or element is None:
                raise FieldRejected(uri, "read-only")
            zones.append(FieldChange(uri, "set", field.from_ews(element, zone)))
    all_day = next((_bool(e.text or "") for u, a, f, e in fields if u == "calendar:IsAllDayEvent" and e is not None), None)
    flag = replace(_flag_from(zone, zones), all_day=all_day)

    values: list[tuple[Field, str, Any]] = []
    for uri, kind, field, element in fields:
        if uri in _ZONES_URIS:
            continue
        value = None if kind == "delete" else field.from_ews(_mandatory(element, uri), flag)
        values.append((field, kind, value))

    reminder_from = any(f.field_uri == "item:ReminderIsSet" and a == "set" and w is False for f, a, w in values)
    result: list[FieldChange] = list(zones)
    for field, kind, value in values:
        if kind == "set" and field.field_uri == "item:ReminderMinutesBeforeStart" and reminder_from:
            continue
        if features < field.min_feature_set:
            if kind == "set" and _is_neutral(value, field.neutral):
                continue
            raise FieldRejected(field.field_uri, "field-disabled")
        if field.classification is FieldClass.DEFAULT_TOLERATED:
            if kind == "set" and _is_neutral(value, field.neutral):
                continue
            raise FieldRejected(field.field_uri, "unknown-field")
        if kind == "delete" and field.field_uri in {"calendar:Start", "calendar:End", "calendar:IsAllDayEvent"}:
            raise FieldRejected(field.field_uri, "read-only")
        result.append(FieldChange(field.field_uri, kind, value))
    return result


def _mandatory(element: etree._Element | None, uri: str) -> etree._Element:
    if element is None:
        raise ValueError(f"value is missing: {uri}")
    return element


def read_changes(item_change: etree._Element, features: FeatureSet, zone: ContextZone) -> list[FieldChange]:
    """`t:ItemChange` -> changes. Unknown, read-only or not enabled fields => FieldRejected, before anything is written."""
    updates = next((k for k in _children(item_change) if _local(k) == "Updates"), None)
    if updates is None:
        raise ValueError("Updates is missing")
    entries: list[tuple[str, str, etree._Element | None]] = []
    for update in _children(updates):
        name = _local(update)
        if name not in {"SetItemField", "DeleteItemField", "AppendToItemField"}:
            raise FieldRejected(name or "?", "unknown-field")
        header = next((k for k in _children(update) if _local(k) in {"FieldURI", "IndexedFieldURI", "ExtendedFieldURI"}), None)
        if name == "AppendToItemField" or header is None or _local(header) != "FieldURI":
            raise FieldRejected(_local(header) if header is not None else "?", "unknown-field")
        uri = header.get("FieldURI") or ""
        if name == "DeleteItemField":
            entries.append((uri, "delete", None))
            continue
        field = FIELDS.get(uri)
        container = next((k for k in _children(update) if _local(k) == "CalendarItem"), None)
        if field is None or field.classification is FieldClass.READ_ONLY:
            entries.append((uri, "set", None))
            continue
        value_element = None if container is None else next((k for k in _children(container) if _local(k) == field.element), None)
        if value_element is None:
            raise ValueError(f"value is missing: {uri}")
        entries.append((uri, "set", value_element))
    return _process(entries, features, zone)


def read_creation(calendar_item: etree._Element, features: FeatureSet, zone: ContextZone) -> tuple[str | None, list[FieldChange]]:
    """`t:CalendarItem` of a CreateItem request -> (UID or None, changes)."""
    uid: str | None = None
    entries: list[tuple[str, str, etree._Element | None]] = []
    for kind in _children(calendar_item):
        name = _local(kind)
        if name == "UID":
            uid = _text(kind) or None
            if uid is not None and (len(uid) > 255 or any(ord(c) < 32 for c in uid)):
                raise ValueError("invalid UID")
            continue
        uri = ELEMENT_TO_URI.get(name)
        if uri is None:
            reason = _reason_unknown(name)
            if reason != "unknown-field" and not _children(kind):
                continue
            raise FieldRejected(f"item:{name}", reason)
        entries.append((uri, "set", kind))
    return uid, _process(entries, features, zone)


def _sorted(changes: Sequence[FieldChange]) -> list[FieldChange]:
    return sorted(changes, key=lambda a: _ORDER.get(a.field_uri, 3))


def _apply(ev: icalendar.Event, changes: Sequence[FieldChange], flag: ContextZone) -> None:
    for a in _sorted(changes):
        FIELDS[a.field_uri].in_ics(ev, a.value if a.kind == "set" else None, flag)


def _check_times(ev: icalendar.Event) -> None:
    if "DTSTART" not in ev:
        raise ValueError("start is missing")
    if "DTEND" not in ev:
        raise ValueError("end is missing")
    start, all_day, _ = _time_value(ev, "DTSTART", runtime_default_zone())
    end, _, _ = _time_value(ev, "DTEND", runtime_default_zone())
    if end < start or (all_day and end <= start):
        raise ValueError("end is before start")


def _add_timezones(calendar: icalendar.Calendar, ev: icalendar.Event) -> None:
    present = {str(c.get("TZID", "")) for c in calendar.walk("VTIMEZONE")}
    requires: set[str] = set()
    span: list[dt.date] = []
    for name in ("DTSTART", "DTEND"):
        if name not in ev:
            continue
        value = ev.decoded(name)
        span.append(value.date() if isinstance(value, dt.datetime) else value)
        identifier = str(getattr(ev[name], "params", {}).get("TZID", ""))
        if identifier and identifier not in present and identifier not in timezones.UTC_NAMES:
            requires.add(identifier)
    if not span:
        return
    for identifier in sorted(requires):
        try:
            vtz = timezones.vtimezone(identifier, min(span) - dt.timedelta(days=366), max(span) + dt.timedelta(days=366))
        except KeyError:
            continue
        calendar.subcomponents.insert(0, vtz)


def _stamp(ev: icalendar.Event, now: dt.datetime) -> None:
    moment = now.astimezone(UTC).replace(microsecond=0)
    _set_nested(ev, "DTSTAMP", moment)
    _set_nested(ev, "LAST-MODIFIED", moment)


def patch(ics: str, changes: Sequence[FieldChange], now: dt.datetime) -> str:
    """Changes only the named properties of the existing object. Everything else (unknown X properties, further VALARMs, ATTENDEE, ...) is preserved."""
    calendar = icalendar.Calendar.from_ical(ics)
    vevents = calendar.walk("VEVENT")
    if len(vevents) != 1 or any(p in vevents[0] for p in SERIES_PROPERTIES) or calendar.walk("VTODO"):
        raise ValueError("object is not changeable")
    ev = vevents[0]
    temporal = any(a.field_uri in _TIME_URIS - _ZONES_URIS for a in changes)
    flag = _flag_from(ContextZone(), changes, ev)
    if temporal and "DURATION" in ev:
        old = _appointment(ev, runtime_default_zone(), None)
        ev.pop("DURATION", None)
        _write_time(ev, "DTEND", old.end, flag.for_end(), bool(old.all_day))
    _apply(ev, changes, flag)
    _set_nested(ev, "SEQUENCE", int(str(ev.get("SEQUENCE", "0")) or 0) + 1)
    _stamp(ev, now)
    if temporal:
        _check_times(ev)
    _add_timezones(calendar, ev)
    return calendar.to_ical().decode("utf-8")


def new(uid: str, fields: Sequence[FieldChange], now: dt.datetime) -> str:
    """New object: TZID + VTIMEZONE, DAVbridge PRODID, no ORGANIZER/ATTENDEE lines."""
    calendar = icalendar.Calendar()
    calendar.add("VERSION", "2.0")
    calendar.add("PRODID", PRODID)
    calendar.add("CALSCALE", "GREGORIAN")
    ev = icalendar.Event()
    ev.add("UID", uid)
    ev.add("SEQUENCE", 0)
    ev.add("X-DAVBRIDGE-CREATED-BY", "outlook")
    moment = now.astimezone(UTC).replace(microsecond=0)
    ev.add("CREATED", moment)
    _stamp(ev, now)
    flag = _flag_from(ContextZone(), fields)
    _apply(ev, fields, flag)
    _check_times(ev)
    calendar.add_component(ev)
    _add_timezones(calendar, ev)
    return calendar.to_ical().decode("utf-8")


def _ical_bytes(value: Any) -> bytes:
    raw = value.to_ical() if hasattr(value, "to_ical") else str(value)
    return raw if isinstance(raw, bytes) else raw.encode("utf-8")


def _signature(component: icalendar.Component, excluded: frozenset[str]) -> tuple:
    own = []
    for name, value in component.items():
        if name.upper() in excluded:
            continue
        for single in _list(value):
            parameter = tuple(sorted((str(k), str(v)) for k, v in getattr(single, "params", {}).items()))
            own.append((name.upper(), parameter, _ical_bytes(single)))
    under = sorted(_signature(k, frozenset()) for k in component.subcomponents)
    return tuple(sorted(own)), tuple(under)


def already_applied(ics: str, changes: Sequence[FieldChange]) -> bool:
    """True if the changes are already contained in the object (a repeated update after the ETag changed)."""
    try:
        applied = patch(ics, changes, _FIXED)
    except (ValueError, KeyError):
        return False
    excluded = frozenset({"DTSTAMP", "LAST-MODIFIED", "SEQUENCE", "DTSTART", "DTEND", "DURATION"})
    try:
        old = icalendar.Calendar.from_ical(ics).walk("VEVENT")[0]
        new_ev = icalendar.Calendar.from_ical(applied).walk("VEVENT")[0]
        equal = _signature(old, excluded) == _signature(new_ev, excluded)
        a, b = read(ics)[0], read(applied)[0]
    except Exception:
        return False
    return equal and (a.start, a.end, a.all_day) == (b.start, b.end, b.all_day)


def _canon(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, (tuple, list)):
        return [_canon(x) for x in value]
    return value


def canonical(fields: Sequence[FieldChange]) -> bytes:
    """Stable byte sequence of the fields (for the dedup key of CreateItem without a UID)."""
    listing = [[a.field_uri, a.kind, _canon(a.value)] for a in sorted(fields, key=lambda a: (a.field_uri, a.kind))]
    return json.dumps(listing, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def href_for_uid(uid: str) -> str:
    """UID + '.ics' if file name and length are acceptable, otherwise sha256(UID)[:40] + '.ics'."""
    candidate = f"{uid}.ics"
    if len(uid) <= 100 and FILE_PATTERN.fullmatch(candidate):
        return candidate
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:40] + ".ics"


# ---------------------------------------------------------------------------------------------------------------------
# Rendering: appointment -> t:CalendarItem
# ---------------------------------------------------------------------------------------------------------------------


def build_calendar_item(
    appointment: Appointment,
    *,
    item_id: str,
    change_key: str,
    folder_id: str,
    folder_change_key: str,
    permissions: PermissionsLike,
    mailbox_name: str,
    mailbox_address: str,
    features: FeatureSet = FeatureSet.BASIC,
    with_body: bool = True,
    only_id: bool = False,
) -> etree._Element:
    """A `t:CalendarItem`. EffectiveRights, IsOrganizer and MyResponseType come only from `permissions` (never as a blanket value)."""
    root = _t("CalendarItem")
    etree.SubElement(root, f"{{{T_NS}}}ItemId").attrib.update({"Id": item_id, "ChangeKey": change_key})
    if only_id:
        return root
    etree.SubElement(root, f"{{{T_NS}}}ParentFolderId").attrib.update({"Id": folder_id, "ChangeKey": folder_change_key})

    def append(element: etree._Element | None) -> None:
        if element is not None:
            root.append(element)

    def simple(name: str, text: str, **attrib: str) -> None:
        root.append(_t(name, text, **attrib))

    simple("ItemClass", "IPM.Appointment")
    simple("Subject", appointment.subject)
    append(_as_sensitivity(appointment) if features >= FeatureSet.DEFAULT else _t("Sensitivity", "Normal"))
    if with_body:
        append(_as_body(appointment))
    if features >= FeatureSet.DEFAULT:
        append(_as_categories(appointment))
    simple("Importance", "Normal")
    simple("DateTimeCreated", _time_text(appointment.created or appointment.changed or appointment.start))
    if appointment.reminder_min is not None:
        simple("ReminderIsSet", "true")
        simple("ReminderMinutesBeforeStart", str(appointment.reminder_min))
    else:
        simple("ReminderIsSet", "false")
    simple("HasAttachments", "false")
    root.append(effective_rights(permissions, folder=False))  # type: ignore[arg-type]
    if appointment.changed:
        simple("LastModifiedTime", _time_text(appointment.changed))
    simple("UID", appointment.uid)
    simple("Start", _time_text(appointment.start))
    simple("End", _time_text(appointment.end))
    simple("IsAllDayEvent", "true" if appointment.all_day else "false")
    simple("LegacyFreeBusyStatus", appointment.free_occupied)
    simple("Location", appointment.location)
    simple("IsMeeting", "true" if appointment.has_attendee or appointment.has_organizer else "false")
    simple("IsCancelled", "true" if appointment.status == "CANCELLED" else "false")
    simple("IsRecurring", "false")
    simple("MeetingRequestWasSent", "false")
    simple("IsResponseRequested", "false")
    simple("IsOrganizer", "true" if permissions.is_organizer else "false")
    simple("CalendarItemType", "Single")
    simple("MyResponseType", "Organizer" if permissions.is_organizer else "Unknown")
    name, address = appointment.organizer or (mailbox_name, mailbox_address)
    organizer = etree.SubElement(root, f"{{{T_NS}}}Organizer")
    mailbox = etree.SubElement(organizer, f"{{{T_NS}}}Mailbox")
    etree.SubElement(mailbox, f"{{{T_NS}}}Name").text = _clean(name)
    etree.SubElement(mailbox, f"{{{T_NS}}}EmailAddress").text = _clean(address)
    etree.SubElement(mailbox, f"{{{T_NS}}}RoutingType").text = "SMTP"
    etree.SubElement(mailbox, f"{{{T_NS}}}MailboxType").text = "Mailbox"
    identifier = _zones_id(appointment)
    simple("TimeZone", identifier)
    simple("AppointmentSequenceNumber", str(appointment.sequence))
    simple("AppointmentState", "0")
    root.append(_t("StartTimeZone", Id=identifier, Name=identifier))
    root.append(_t("EndTimeZone", Id=identifier, Name=identifier))
    return root
