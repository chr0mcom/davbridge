"""Appointment <-> AirSync `ApplicationData` for calendar objects (EAS 14.1). Pure: no I/O, no asyncio.

Reading: `appointment_to_application_data` builds the node tree from an `ical.Appointment` (for series from `recurrence.read_series`) in the
fixed element order EAS expects (Timezone, AllDayEvent, BusyStatus, Categories, DtStamp, EndTime, Location, MeetingStatus, OrganizerEmail,
OrganizerName, Recurrence, Reminder, Sensitivity, Subject, StartTime, UID, Exceptions, airsyncbase:Body). `None` means: not representable, hence invisible.

Writing: `application_data_to_changes` turns the client's node tree into `ical.FieldChange` lists with the EWS `field_uri` values and the value
types that `ical.create`/`ical.patch` expect (Subject str, Body `("text"|"html", str)`, Start/End timezone-aware UTC datetime, IsAllDayEvent bool,
zones as IANA names, LegacyFreeBusyStatus, Sensitivity `Normal|Personal|Private|Confidential`, Categories tuple,
ReminderIsSet/ReminderMinutesBeforeStart). The write path itself is `ical` (it preserves unknown X properties, further VALARMs, X-ALT-DESC, VTIMEZONE).

Details (all additive or deliberately narrower than the plain protocol):
- `appointment_to_application_data(..., default_timezone=...)`: optional keyword (default: the configured `default_timezone`); it must match the
  `ical.read` of the caller. For series the master is read from the object (`Series.master`); `appointment` then only serves as an entry check (reason `series`).
- `application_data_to_changes(..., current=...)`: optional current `Appointment` (for Change only). Unchanged fields are then dropped.
  This protects against data loss, because clients often send the whole object back on a change (for example the unchanged text body would
  discard `X-ALT-DESC`, a re-sent start would overwrite the zone of the appointment). Without `current` all sent fields are written.
- The UID is not a `FieldChange` (`ical.create` takes it separately): `application_data_uid(data)` returns it (or `None`, or `Rejected("uid")`).
- `omitted_fields(data)` names the expected fields that are missing from a Change (counter `eas_change_skipped`).
- As for Subject, Location and Categories, an explicitly empty `Reminder` or `Body/Data` element means "clear the field".
- All-day incoming: the date is determined in the blob zone. If no IANA zone matches, it is derived from the block's fixed offset
  (standard or daylight offset, whichever yields midnight) and written as midnight of the default zone (the day is preserved).
- Series are never changeable (`eas_permissions`); Add/Change with `Recurrence`/`Exceptions` is `Rejected`.
"""

from __future__ import annotations

import datetime as dt
import html as html_module
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import timezones
from ..config import runtime_default_zone
from ..ical import Appointment, FieldChange
from ..permissions import NONE, Permissions, element_permissions
from . import recurrence, tags, timezone
from .wbxml import Node

UTC: Final = dt.timezone.utc
MAX_CATEGORIES: Final = 300
MAX_CATEGORY_CHAR: Final = 300
MAX_UID_OUTPUT: Final = 300
MAX_UID_INPUT: Final = 255
YEAR_FROM: Final = 1900
YEAR_UNTIL: Final = 2999

_TIME: Final = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$")
_BUSY: Final = MappingProxyType({0: "Free", 1: "Tentative", 2: "Busy", 3: "OOF", 4: "Busy"})  # type 4 does not exist in 14.1
_SENS: Final = MappingProxyType({0: "Normal", 1: "Personal", 2: "Private", 3: "Confidential"})
_CLASSIFICATION: Final = MappingProxyType({"Normal": "PUBLIC", "Personal": "PRIVATE", "Private": "PRIVATE", "Confidential": "CONFIDENTIAL"})
_NEUTRAL: Final = frozenset(
    {
        "Calendar:OrganizerName",
        "Calendar:OrganizerEmail",
        "Calendar:MeetingStatus",
        "Calendar:ResponseRequested",
        "Calendar:DisallowNewTimeProposal",
        "Calendar:ClientUid",
        "Calendar:AppointmentReplyTime",
        "Calendar:ResponseType",
        "Calendar:DtStamp",
        "Calendar:UID",
        "Calendar:Attendees",
        "Calendar:Recurrence",
        "Calendar:Exceptions",
    }
)
_ALLOWED: Final = frozenset(
    {
        "Calendar:Subject",
        "Calendar:Location",
        "AirSyncBase:Body",
        "Calendar:StartTime",
        "Calendar:EndTime",
        "Calendar:AllDayEvent",
        "Calendar:Timezone",
        "Calendar:BusyStatus",
        "Calendar:Sensitivity",
        "Calendar:Reminder",
        "Calendar:Categories",
    }
)
_ZONES_URIS: Final = frozenset({"calendar:StartTimeZone", "calendar:EndTimeZone"})
_TIME_URIS: Final = frozenset({"calendar:Start", "calendar:End", "calendar:IsAllDayEvent"})
_EXPECTED_ON_CHANGE: Final = (
    "Calendar:Timezone",
    "Calendar:AllDayEvent",
    "Calendar:BusyStatus",
    "Calendar:DtStamp",
    "Calendar:EndTime",
    "Calendar:Sensitivity",
    "Calendar:Subject",
    "Calendar:StartTime",
)


class Rejected(Exception):
    """The client command is not applied. Only the fixed reason, never content."""

    def __init__(self, reason: Literal["series", "attendees", "exception", "time", "uid", "unknown-field"]) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------------------------------------------------


def eas_permissions(folder: Permissions, appointment: Appointment, is_series: bool) -> Permissions:
    """Series are readable but never changeable or deletable; single appointments follow `permissions.element_permissions`."""
    if is_series:
        if not folder.read:
            return NONE
        return Permissions(read=True, change=False, delete=False, create=folder.create, is_organizer=False)
    return element_permissions(folder, appointment)


# ---------------------------------------------------------------------------------------------------------------------
# Reading: appointment -> ApplicationData
# ---------------------------------------------------------------------------------------------------------------------


def _sheet(name: str, value: object | None) -> Node | None:
    return tags.leaf(f"Calendar:{name}", value)


def _timezone_node(t: Appointment, series_necessary: bool) -> Node | None:
    """Base64 block of the appointment zone. For series the zone is part of the meaning (unknown => None), otherwise UTC is an acceptable fallback."""
    try:
        return _sheet("Timezone", timezone.blob_base64(t.zone, t.start))
    except KeyError:
        return None if series_necessary else _sheet("Timezone", timezone.blob_base64("UTC", t.start))


def _all_day_end(t: Appointment) -> dt.datetime:
    if t.end > t.start:
        return t.end
    try:
        zone: dt.tzinfo = UTC if t.zone in timezones.UTC_NAMES else ZoneInfo(t.zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        zone = UTC
    local = t.start.astimezone(zone).date() + dt.timedelta(days=1)
    return dt.datetime.combine(local, dt.time(), zone).astimezone(UTC)


def _html_to_text(html: str) -> str:
    replaces = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", html)
    return html_module.unescape(re.sub(r"<[^>]+>", "", replaces)).strip("\n")


def _crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _truncate(text: str, char: int, byte: int | None) -> tuple[str, bool]:
    """Shorten text to at most `chars` characters and `byte` UTF-8 bytes without tearing a character."""
    truncated = False
    if len(text) > char:
        text, truncated = text[: max(char, 0)], True
    if byte is not None and len(text.encode("utf-8")) > byte:
        text, truncated = text.encode("utf-8")[: max(byte, 0)].decode("utf-8", errors="ignore"), True
    return text, truncated


def _body(t: Appointment, body_types: Sequence[int], trunc: int | None, body_max: int) -> Node | None:
    """`airsyncbase:Body`: HTML only for BodyPreference 2 with existing HTML, otherwise text (type 1); types 3/4 never."""
    if 2 in body_types and t.html:
        kind, content = 2, t.html
    else:
        text = t.text or (_html_to_text(t.html) if t.html else "")
        kind, content = 1, _crlf(text)
    if not content:
        return None
    total = len(content.encode("utf-8"))
    visible, truncated = _truncate(content, body_max, trunc)
    return tags.node(
        "AirSyncBase:Body",
        tags.leaf("AirSyncBase:Type", kind),
        tags.leaf("AirSyncBase:EstimatedDataSize", total),
        tags.leaf("AirSyncBase:Truncated", 1 if truncated else 0),
        tags.node("AirSyncBase:Data", text=visible),
    )


def appointment_to_application_data(
    appointment: Appointment, ics: str, *, body_types: Sequence[int], trunc: int | None, body_max: int, account_name: str, account_address: str, default_timezone: str | None = None
) -> Node | None:
    """`AirSync:ApplicationData` for an appointment, or `None` if it is not (yet) representable."""
    repetition = None
    exceptions: tuple[Node, ...] = ()
    t = appointment
    if appointment.reason == "series":
        read_back = recurrence.read_series(ics, default_timezone)
        if read_back is None or read_back.master is None:
            return None
        t, repetition, exceptions = read_back.master, read_back.repetition, read_back.exceptions
    elif not appointment.representable or recurrence.series_line_unreadable(ics):
        return None
    if not t.uid or len(t.uid) > MAX_UID_OUTPUT:
        return None
    zone_node = _timezone_node(t, series_necessary=repetition is not None)
    if zone_node is None:
        return None
    end = _all_day_end(t) if t.all_day else t.end
    name, address = t.organizer or (account_name, account_address)
    categories = None
    if t.categories:
        categories = tags.node("Calendar:Categories", *(tags.leaf("Calendar:Category", k[:MAX_CATEGORY_CHAR]) for k in t.categories[:MAX_CATEGORIES]))
    return tags.node(
        "AirSync:ApplicationData",
        zone_node,
        _sheet("AllDayEvent", 1 if t.all_day else 0),
        _sheet("BusyStatus", recurrence.BUSY_STATUS.get(t.free_occupied, 2)),
        categories,
        _sheet("DtStamp", recurrence.compact(t.changed or t.created or t.start)),
        _sheet("EndTime", recurrence.compact(end)),
        _sheet("Location", t.location) if t.location else None,
        _sheet("MeetingStatus", 0),
        _sheet("OrganizerEmail", address),
        _sheet("OrganizerName", name),
        repetition,
        _sheet("Reminder", t.reminder_min),
        _sheet("Sensitivity", recurrence.SENSITIVITY.get(t.classification, 0)),
        _sheet("Subject", t.subject),
        _sheet("StartTime", recurrence.compact(t.start)),
        _sheet("UID", t.uid),
        tags.node("Calendar:Exceptions", *exceptions) if exceptions else None,
        _body(t, body_types, trunc, body_max),
    )


# ---------------------------------------------------------------------------------------------------------------------
# Writing: ApplicationData -> FieldChange
# ---------------------------------------------------------------------------------------------------------------------


def _text(k: Node | None) -> str:
    return (k.text or "") if k is not None else ""


def _time(k: Node) -> dt.datetime:
    hit = _TIME.match(_text(k).strip())
    if hit is None:
        raise Rejected("time")
    try:
        value = dt.datetime(*(int(x) for x in hit.groups()), tzinfo=UTC)
    except ValueError:
        raise Rejected("time") from None
    if not YEAR_FROM <= value.year <= YEAR_UNTIL:
        raise Rejected("time")
    return value


def _whole(k: Node, minimum: int, maximum: int, reason: Literal["time", "unknown-field"] = "unknown-field") -> int:
    try:
        number = int(_text(k).strip())
    except ValueError:
        raise Rejected(reason) from None
    if not minimum <= number <= maximum:
        raise Rejected(reason)
    return number


def _valid_uid(value: str) -> str:
    if not value or len(value) > MAX_UID_INPUT or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Rejected("uid")
    return value


def _children_after_name(data: Node) -> dict[str, Node]:
    """First occurrence per name; unknown tokens are named `Page:0xNN`."""
    result: dict[str, Node] = {}
    for kind in data.children:
        result.setdefault(tags.display(kind), kind)
    return result


def application_data_uid(data: Node) -> str | None:
    """UID of the client (Add only), validated; `None` if none was sent. Invalid => `Rejected("uid")`."""
    k = tags.find(data, "Calendar:UID")
    if k is None or not _text(k):
        return None
    return _valid_uid(_text(k))


def omitted_fields(data: Node) -> list[str]:
    """Names of the fields normally sent along that are missing from a Change (for counting only; they change nothing)."""
    present = _children_after_name(data)
    return [n for n in _EXPECTED_ON_CHANGE if n not in present]


def _zone_text(name: str) -> dt.tzinfo:
    return UTC if name in timezones.UTC_NAMES else ZoneInfo(name)


def _blob_zone(blob: Node | None, reference: dt.datetime) -> tuple[str | None, timezone.Timezone | None]:
    """(IANA name or None, decoded block or None). Without a block or with an unreadable one: (None, None)."""
    text = _text(blob).strip()
    if not text:
        return None, None
    try:
        raw = timezone.blob_from_base64(text)
        block = timezone.decode(text)
    except ValueError:
        return None, None
    return timezone.iana_for_blob(raw, reference), block


def _date_from_block(instant: dt.datetime, block: timezone.Timezone) -> dt.date:
    """Local calendar day of a point in time at the block's fixed offset; prefers the offset that yields midnight."""
    offsets = [-(block.bias + block.default_bias)]
    if block.summer.month:
        offsets.append(-(block.bias + block.summer_bias))
    for minutes in offsets:
        local = instant + dt.timedelta(minutes=minutes)
        if (local.hour, local.minute, local.second) == (0, 0, 0):
            return local.date()
    return (instant + dt.timedelta(minutes=offsets[0])).date()


def _default_zones_midnight(tag: dt.date) -> dt.datetime:
    return dt.datetime.combine(tag, dt.time(), ZoneInfo(runtime_default_zone())).astimezone(UTC)


def _current_date(instant: dt.datetime, current: Appointment) -> dt.date:
    try:
        zone = _zone_text(current.zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        zone = UTC
    return instant.astimezone(zone).date()


def _categories(k: Node) -> tuple[str, ...]:
    names = [_text(c).strip() for c in tags.find_all(k, "Calendar:Category")]
    return tuple(n[:MAX_CATEGORY_CHAR] for n in names if n)[:MAX_CATEGORIES]


def _body_change(k: Node) -> FieldChange | None:
    kind_node = tags.find(k, "AirSyncBase:Type")
    kind = _whole(kind_node, 0, 99) if kind_node is not None and _text(kind_node).strip() else 1
    data = tags.find(k, "AirSyncBase:Data")
    if kind not in (1, 2) or data is None or _text(tags.find(k, "AirSyncBase:Truncated")).strip() == "1":
        return None
    content = _lf(_text(data))
    if not content:
        return FieldChange("item:Body", "delete", None)
    return FieldChange("item:Body", "set", ("html" if kind == 2 else "text", content))


def _unchanged_body(f: FieldChange, current: Appointment, truncation: int | None) -> bool:
    """Equal, or an echo of a delivery that DAVbridge truncated. A truncation only counts if the full text is longer than the
    delivered limit, the text sent back is at most that limit long and remains a prefix of the full text; a text
    below the limit is a deliberate shortening by the user and is written."""
    if f.kind == "delete":
        return not current.text and not current.html
    kind, content = f.value
    comparison = _lf(current.html if kind == "html" else current.text or "")
    if comparison == content:
        return True
    if kind != "text" or truncation is None:
        return False
    candidates = [_lf(current.text or "")]
    if current.html:
        candidates.append(_lf(_html_to_text(current.html)))
    for full in candidates:
        body = content.rstrip("…").rstrip()
        if full and len(full) > truncation and len(content) <= truncation + 1 and 2 * len(body) >= truncation and full.startswith(body):
            return True
    return False


GHOSTBAR_DELETE: Final[Mapping[str, FieldChange]] = MappingProxyType(
    {
        "Calendar:Subject": FieldChange("item:Subject", "delete", None),
        "Calendar:Location": FieldChange("calendar:Location", "delete", None),
        "Calendar:Categories": FieldChange("item:Categories", "delete", None),
        "Calendar:Reminder": FieldChange("item:ReminderIsSet", "set", False),
        "Calendar:Sensitivity": FieldChange("item:Sensitivity", "set", "Normal"),
        "Calendar:BusyStatus": FieldChange("calendar:LegacyFreeBusyStatus", "set", "Busy"),
    }
)


def application_data_to_changes(
    data: Node, *, new: bool, current: Appointment | None = None, not_ghosted: frozenset[str] = frozenset(), truncation: int | None = None
) -> tuple[list[FieldChange], list[str]]:
    """`ApplicationData` of an Add/Change -> (FieldChanges, names of ignored unknown elements). Raises `Rejected`.

    Only the allowlist applies. Neutral elements (organizer, MeetingStatus, DtStamp, empty attendee list, ...) are silently ignored,
    unknown ones are reported by name. `Attendees` with attendees, and `Recurrence` and `Exceptions` with content, are rejected. Add requires a valid
    start and a valid end.

    Missing fields in a Change (MS-ASCMD 2.2.3.24 for 14.1): a missing field named in `not_ghosted` is deleted
    (`GHOSTBAR_DELETE`), but only if the Change is a full image (StartTime and EndTime present). A 16.x style delta without times
    never deletes anything (protection against data loss if the Microsoft service sends deltas). Body, Exceptions and times stay if missing."""
    children = _children_after_name(data)
    ignored = [n for n in children if n not in _NEUTRAL and n not in _ALLOWED]
    attendee = children.get("Calendar:Attendees")
    if attendee is not None and attendee.children:
        raise Rejected("attendees")
    repetition = children.get("Calendar:Recurrence")
    if repetition is not None and repetition.children:
        raise Rejected("series")
    exceptions = children.get("Calendar:Exceptions")
    if exceptions is not None and exceptions.children:
        raise Rejected("exception")
    if new:
        application_data_uid(data)

    start_k, end_k = children.get("Calendar:StartTime"), children.get("Calendar:EndTime")
    start = _time(start_k) if start_k is not None else None
    end = _time(end_k) if end_k is not None else None
    if new and (start is None or end is None):
        raise Rejected("time")
    all_day_k = children.get("Calendar:AllDayEvent")
    all_day: bool | None = None
    if all_day_k is not None:
        all_day = {0: False, 1: True}.get(_whole(all_day_k, 0, 1, "time"))
    if new and all_day is None:
        all_day = False
    is_all_day = all_day if all_day is not None else (current.all_day if current is not None else False)

    time_fields = _time_fields(children, start, end, all_day, is_all_day, new, current)
    fields: list[FieldChange] = []
    if "Calendar:Subject" in children:
        subject = _text(children["Calendar:Subject"])
        fields.append(FieldChange("item:Subject", "set", subject) if subject or new else FieldChange("item:Subject", "delete", None))
    if "Calendar:Location" in children:
        location = _text(children["Calendar:Location"])
        fields.append(FieldChange("calendar:Location", "set", location) if location else FieldChange("calendar:Location", "delete", None))
    if "AirSyncBase:Body" in children:
        change = _body_change(children["AirSyncBase:Body"])
        if change is not None:
            fields.append(change)
    fields.extend(time_fields)
    if "Calendar:BusyStatus" in children:
        fields.append(FieldChange("calendar:LegacyFreeBusyStatus", "set", _BUSY[_whole(children["Calendar:BusyStatus"], 0, 4)]))
    if "Calendar:Sensitivity" in children:
        fields.append(FieldChange("item:Sensitivity", "set", _SENS[_whole(children["Calendar:Sensitivity"], 0, 3)]))
    if "Calendar:Categories" in children:
        names = _categories(children["Calendar:Categories"])
        fields.append(FieldChange("item:Categories", "set", names) if names else FieldChange("item:Categories", "delete", None))
    if "Calendar:Reminder" in children:
        text = _text(children["Calendar:Reminder"]).strip()
        if text:
            minutes = _whole(children["Calendar:Reminder"], 0, 40320 * 10)
            fields.append(FieldChange("item:ReminderIsSet", "set", True))
            fields.append(FieldChange("item:ReminderMinutesBeforeStart", "set", minutes))
        else:
            fields.append(FieldChange("item:ReminderIsSet", "set", False))

    if not new and not_ghosted and "Calendar:StartTime" in children and "Calendar:EndTime" in children:
        for name, deletion in GHOSTBAR_DELETE.items():
            if name in not_ghosted and name not in children:
                fields.append(deletion)
    if new:
        fields = [f for f in fields if f.kind == "set"]
    elif current is not None:
        fields = [f for f in fields if not _unchanged(f, current, truncation)]
        if not any(f.field_uri in _TIME_URIS for f in fields):
            fields = [f for f in fields if f.field_uri not in _ZONES_URIS]
    return fields, ignored


def _time_fields(children: dict[str, Node], start: dt.datetime | None, end: dt.datetime | None, all_day: bool | None, is_all_day: bool, new: bool, current: Appointment | None) -> list[FieldChange]:
    if start is None and end is None and all_day is None:
        return []
    reference = start or end or dt.datetime(2026, 1, 1, tzinfo=UTC)
    iana, block = _blob_zone(children.get("Calendar:Timezone"), reference)
    zone_name = iana or runtime_default_zone()
    fields: list[FieldChange] = []
    zones_necessary = new or is_all_day or (current is not None and current.all_day)
    if is_all_day and (start is not None or end is not None):
        days: list[dt.date | None] = []
        for instant in (start, end):
            if instant is None:
                days.append(None)
            elif iana is not None or block is None:
                days.append(instant.astimezone(_zone_text(zone_name)).date())
            else:
                days.append(_date_from_block(instant, block))
        if iana is None and block is not None:
            zone_name = runtime_default_zone()
        if days[0] is not None and days[1] is not None and days[1] <= days[0]:
            raise Rejected("time")
        if new and (days[0] is None or days[1] is None):
            raise Rejected("time")
        zone = _zone_text(zone_name)
        if current is not None and current.all_day:
            so_far = (_current_date(current.start, current), _current_date(current.end, current))
            days = [None if d == old else d for d, old in zip(days, so_far)]
        values = [dt.datetime.combine(d, dt.time(), zone).astimezone(UTC) if d is not None else None for d in days]
        start, end = values[0], values[1]
    elif start is not None and end is not None and end < start:
        raise Rejected("time")
    if all_day is not None:
        fields.append(FieldChange("calendar:IsAllDayEvent", "set", all_day))
    if start is not None:
        fields.append(FieldChange("calendar:Start", "set", start))
    if end is not None:
        fields.append(FieldChange("calendar:End", "set", end))
    if zones_necessary and (start is not None or end is not None):
        fields.append(FieldChange("calendar:StartTimeZone", "set", zone_name))
        fields.append(FieldChange("calendar:EndTimeZone", "set", zone_name))
    return fields


def _unchanged(f: FieldChange, current: Appointment, truncation: int | None = None) -> bool:
    """True if the change would not modify the current appointment (protection against clients sending unchanged fields back)."""
    uri = f.field_uri
    if uri == "item:Subject":
        return (f.value or "") == current.subject
    if uri == "calendar:Location":
        return (f.value or "") == current.location
    if uri == "item:Body":
        return _unchanged_body(f, current, truncation)
    if uri == "calendar:IsAllDayEvent":
        return f.value == current.all_day
    if uri in ("calendar:Start", "calendar:End"):
        return f.value == (current.start if uri == "calendar:Start" else current.end)
    if uri in ("calendar:StartTimeZone", "calendar:EndTimeZone"):
        return False
    if uri == "calendar:LegacyFreeBusyStatus":
        return f.value == current.free_occupied
    if uri == "item:Sensitivity":
        return _CLASSIFICATION[f.value] == (current.classification if current.classification in ("PRIVATE", "CONFIDENTIAL") else "PUBLIC")
    if uri == "item:Categories":
        return tuple(f.value or ()) == current.categories
    if uri == "item:ReminderIsSet":
        return f.value == (current.reminder_min is not None)
    if uri == "item:ReminderMinutesBeforeStart":
        return f.value == current.reminder_min
    return False
