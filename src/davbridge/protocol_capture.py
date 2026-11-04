"""Capture mode: structure skeleton of every request of the test account, without text values (allowlist).

Pure module: no asyncio import, no file I/O of its own. `ProtocolCapture` hands lines to an injected `Storage`
(event_log.FileStorage) that writes without blocking. Anything not explicitly allowed appears only as a class.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable, Final, Mapping, Protocol, Sequence

from lxml import etree

from .soap import MESSAGES_NS, SOAP_NS, TYPES_NS, Request

if TYPE_CHECKING:  # types only; the main strand supplies the configuration and clock modules
    from .clock import Clock
    from .config import Account

DEPTH_MAX: Final = 40
CHILDREN_MAX: Final = 500

_VALUE: Final = re.compile(r"[A-Za-z0-9:_\-]{1,64}")
_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]{0,63}")
_NS_OK: Final = re.compile(r"[A-Za-z0-9:/._#\-]{1,200}")
_NUMBER: Final = re.compile(r"[0-9]{1,9}")
_TIME: Final = re.compile(r"\d{4}-\d{2}-\d{2}(T[\d:.]{1,20}(Z|[+-]\d{2}:\d{2})?)?")
_UUID: Final = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_GUID_BRACKET: Final = re.compile(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}")
_HEX: Final = re.compile(r"[0-9A-Fa-f]{16,}")
_B64URL: Final = re.compile(r"[A-Za-z0-9_\-+/=]{16,}")
_FIELD_URI: Final = re.compile(r"[a-z]{3,20}:[A-Za-z0-9]{1,60}")
_FOLDER_NAME: Final = re.compile(r"[a-z]{1,40}")
_PROP_TAG: Final = re.compile(r"(0x[0-9A-Fa-f]{1,4}|[0-9]{1,6})")
_WIN_ZONE: Final = re.compile(r"(UTC|[A-Za-z.() ,\-]{1,50} (Standard|Daylight) Time)")
_UA_TOKEN: Final = re.compile(r"[A-Za-z][A-Za-z0-9._\-]{0,40}/[0-9][0-9A-Za-z._\-]{0,30}")
_HEADER_NAME: Final = re.compile(r"[a-z0-9\-]{1,40}")
_MEDIA: Final = re.compile(r"[a-z]{1,20}/[a-z0-9.+\-]{1,40}")
_ACTION: Final = re.compile(r"https?://schemas\.microsoft\.com/exchange/services/2006/(messages|types)/([A-Za-z]{1,64})")

_PREFIX: Final = MappingProxyType({SOAP_NS: "s", MESSAGES_NS: "m", TYPES_NS: "t"})

# Closed enumerations: only these values appear in clear text.
_ENUM: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "BaseShape": frozenset({"IdOnly", "Default", "AllProperties"}),
        "EventType": frozenset({"CopiedEvent", "CreatedEvent", "DeletedEvent", "ModifiedEvent", "MovedEvent", "NewMailEvent", "FreeBusyChangedEvent", "StatusEvent"}),
        "LegacyFreeBusyStatus": frozenset({"Free", "Tentative", "Busy", "OOF", "WorkingElsewhere", "NoData"}),
        "Sensitivity": frozenset({"Normal", "Personal", "Private", "Confidential"}),
        "Importance": frozenset({"Low", "Normal", "High"}),
        "BodyType": frozenset({"Text", "HTML", "Best"}),
        "Traversal": frozenset({"Shallow", "Deep", "SoftDeleted", "Associated"}),
        "ConflictResolution": frozenset({"NeverOverwrite", "AutoResolve", "AlwaysOverwrite"}),
        "MessageDisposition": frozenset({"SaveOnly", "SendOnly", "SendAndSaveCopy"}),
        "SendMeetingInvitationsOrCancellations": frozenset({"SendToNone", "SendOnlyToAll", "SendOnlyToChanged", "SendToAllAndSaveCopy", "SendToChangedAndSaveCopy"}),
        "SendMeetingInvitations": frozenset({"SendToNone", "SendOnlyToAll", "SendToAllAndSaveCopy"}),
        "SendMeetingCancellations": frozenset({"SendToNone", "SendOnlyToAll", "SendToAllAndSaveCopy"}),
        "DeleteType": frozenset({"HardDelete", "SoftDelete", "MoveToDeletedItems"}),
        "AffectedTaskOccurrences": frozenset({"AllOccurrences", "SpecifiedOccurrenceOnly"}),
        "DistinguishedPropertySetId": frozenset({"Common", "Address", "Appointment", "CalendarAssistant", "Meeting", "PublicStrings", "Task", "UnifiedMessaging", "InternetHeaders"}),
        "PropertyType": frozenset(
            {
                "Short",
                "Integer",
                "Long",
                "Float",
                "Double",
                "Boolean",
                "String",
                "SystemTime",
                "Binary",
                "Currency",
                "ApplicationTime",
                "CLSID",
                "Object",
                "Error",
                "IntegerArray",
                "StringArray",
                "SystemTimeArray",
                "BinaryArray",
            }
        ),
    }
)
# Allowed value attributes: (element, attribute) -> check (enumeration from _ENUM under the attribute name, otherwise pattern).
_VALUE_ATTRIBUTE: Final[Mapping[tuple[str, str], re.Pattern[str] | None]] = MappingProxyType(
    {
        ("FieldURI", "FieldURI"): _FIELD_URI,
        ("IndexedFieldURI", "FieldURI"): _FIELD_URI,
        ("FindItem", "Traversal"): None,
        ("FindFolder", "Traversal"): None,
        ("UpdateItem", "ConflictResolution"): None,
        ("UpdateItem", "MessageDisposition"): None,
        ("UpdateItem", "SendMeetingInvitationsOrCancellations"): None,
        ("CreateItem", "MessageDisposition"): None,
        ("CreateItem", "SendMeetingInvitations"): None,
        ("DeleteItem", "DeleteType"): None,
        ("DeleteItem", "SendMeetingCancellations"): None,
        ("DeleteItem", "AffectedTaskOccurrences"): None,
        ("Body", "BodyType"): None,
        ("DistinguishedFolderId", "Id"): _FOLDER_NAME,
        ("ExtendedFieldURI", "PropertyTag"): _PROP_TAG,
        ("ExtendedFieldURI", "PropertySetId"): _UUID,
        ("ExtendedFieldURI", "DistinguishedPropertySetId"): None,
        ("ExtendedFieldURI", "PropertyType"): None,
        ("ExtendedFieldURI", "PropertyId"): _NUMBER,
        ("ExtendedFieldURI", "PropertyName"): _VALUE,
        ("RequestServerVersion", "Version"): _VALUE,
        ("TimeZoneDefinition", "Id"): _WIN_ZONE,
    }
)
# Elements whose text may appear as a value (enumeration from _ENUM or number).
_VALUE_ELEMENTS: Final = frozenset(_ENUM) - {
    "Traversal",
    "ConflictResolution",
    "MessageDisposition",
    "DeleteType",
    "SendMeetingInvitationsOrCancellations",
    "SendMeetingInvitations",
    "SendMeetingCancellations",
    "AffectedTaskOccurrences",
    "DistinguishedPropertySetId",
    "PropertyType",
}
_NUMBER_ELEMENTS: Final = frozenset({"MaxChangesReturned", "ConnectionTimeout", "Timeout", "MaxEntriesReturned", "Offset", "ReminderMinutesBeforeStart"})
_NUMBER_ATTRIBUTE: Final = frozenset({"MaxEntriesReturned", "MaxChangesReturned", "Offset", "Timeout"})
_ID_ELEMENTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "SyncState": "sync",
        "SubscriptionId": "subscription",
        "Watermark": "watermark",
    }
)
_ID_ATTRIBUTE: Final[Mapping[tuple[str, str], str]] = MappingProxyType(
    {
        ("ItemId", "Id"): "element",
        ("FolderId", "Id"): "folder",
        ("ParentFolderId", "Id"): "folder",
        ("ItemId", "ChangeKey"): "changekey",
        ("FolderId", "ChangeKey"): "changekey",
        ("ParentFolderId", "ChangeKey"): "changekey",
        ("OccurrenceItemId", "RecurringMasterId"): "element",
        ("OccurrenceItemId", "ChangeKey"): "changekey",
        ("RecurringMasterItemId", "OccurrenceId"): "element",
        ("RecurringMasterItemId", "ChangeKey"): "changekey",
    }
)
_ID_ATTRIBUTE_NAMES: Final = frozenset({"Id", "ChangeKey", "RecurringMasterId", "OccurrenceId"})

_HEADER_NEVER: Final = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port"}
)
_TIME_WINDOW: Final = frozenset({"start_days", "duration_days"})

IdChecker = Callable[[str, str], bool]


@dataclass(frozen=True, slots=True)
class Skeleton:
    operation: str
    tree: tuple  # (qname, ((attr, class_or_value), ...), (children...)); element text as pseudo attribute "#text"
    header: tuple[tuple[str, str], ...]
    fingerprint: str  # sha256 over the tree without time window classes, 16 hex digits


class Storage(Protocol):
    def write(self, path: Path, text: str, *, one_time: bool = False) -> None: ...


# ---------------------------------------------------------------- classes


def _length(n: int) -> str:
    return "1-16" if n <= 16 else "17-256" if n <= 256 else "257+"


def classification_text(value: str) -> str:
    n = len(value.strip())
    return "<empty>" if n == 0 else f"<text:{_length(n)}>"


def _classification_uid(value: str) -> str:
    w = value.strip()
    if _UUID.fullmatch(w):
        return "<uid:uuid>"
    if _GUID_BRACKET.fullmatch(w):
        return "<uid:guid-brace>"
    if _HEX.fullmatch(w):
        return "<uid:hex>"
    return f"<uid:other:{min(len(w), 200)}>"


def _classification_id(kind: str, value: str, checker: IdChecker | None) -> str:
    w = value.strip()
    if not w:
        return "<empty>"
    try:
        ok = bool(checker(kind, w)) if checker is not None else bool(_B64URL.fullmatch(w))
    except Exception:  # the checker must never disturb the capture
        ok = False
    return "<id:valid>" if ok else "<id:invalid>"


def _number_or_classification(value: str) -> str:
    return value if _NUMBER.fullmatch(value) else classification_text(value)


def _qname(tag: object) -> str:
    if not isinstance(tag, str):
        return "<node>"
    q = etree.QName(tag)
    local = q.localname if _NAME.fullmatch(q.localname) else "<name>"
    if q.namespace is None:
        return local
    prefix = _PREFIX.get(q.namespace)
    if prefix:
        return f"{prefix}:{local}"
    ns = q.namespace if _NS_OK.fullmatch(q.namespace) else "<ns>"
    return f"{{{ns}}}{local}"


def _local(tag: object) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _attr_value(el: str, name: str, value: str, checker: IdChecker | None) -> str:
    key = (el, name)
    if key in _VALUE_ATTRIBUTE:
        pattern = _VALUE_ATTRIBUTE[key]
        if pattern is None:
            allowed = _ENUM.get(name)
            return value if allowed is not None and value in allowed else classification_text(value)
        return value if pattern.fullmatch(value) else classification_text(value)
    if key in _ID_ATTRIBUTE:
        return _classification_id(_ID_ATTRIBUTE[key], value, checker)
    if name in _ID_ATTRIBUTE_NAMES:
        return _classification_id("element" if name != "ChangeKey" else "changekey", value, checker)
    if value in ("true", "false"):
        return value
    if name in _NUMBER_ATTRIBUTE:
        return _number_or_classification(value)
    if _TIME.fullmatch(value.strip()):
        return "<time>"
    return classification_text(value)


def _text_value(el: str, text: str, checker: IdChecker | None) -> str:
    w = text.strip()
    if el in _VALUE_ELEMENTS:
        return w if w in _ENUM[el] else classification_text(w)
    if el in _NUMBER_ELEMENTS:
        return _number_or_classification(w)
    if el in _ID_ELEMENTS:
        return _classification_id(_ID_ELEMENTS[el], w, checker)
    if el == "UID":
        return _classification_uid(w)
    if w in ("true", "false"):
        return w
    if _TIME.fullmatch(w):
        return "<time>"
    return classification_text(w)


def _days(delta_s: float) -> str:
    return str(round(delta_s / 86400))


def _time_read(value: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _calendar_view_attribute(e: etree._Element, now: datetime) -> list[tuple[str, str]]:
    """Only relative time windows, no absolute dates."""
    out: list[tuple[str, str]] = []
    start = _time_read(e.get("StartDate", ""))
    end = _time_read(e.get("EndDate", ""))
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    out.append(("start_days", _days((start - now).total_seconds()) if start else "<unreadable>"))
    out.append(("duration_days", _days((end - start).total_seconds()) if start and end else "<unreadable>"))
    if "MaxEntriesReturned" in e.attrib:
        out.append(("MaxEntriesReturned", _number_or_classification(e.get("MaxEntriesReturned", ""))))
    return out


def _node(e: etree._Element, now: datetime, checker: IdChecker | None, depth: int) -> tuple:
    local = _local(e.tag)
    attribute: list[tuple[str, str]] = []
    if local == "CalendarView":
        attribute.extend(_calendar_view_attribute(e, now))
        remaining = [k for k in e.attrib if k not in ("StartDate", "EndDate", "MaxEntriesReturned")]
    else:
        remaining = list(e.attrib)
    for k in remaining:
        name = _qname(k)
        attribute.append((name, _attr_value(local, _local(k), str(e.attrib[k]), checker)))
    children_elements = [k for k in e if isinstance(k.tag, str)]
    text = e.text or ""
    if not children_elements:
        if text.strip():
            attribute.append(("#text", _text_value(local, text, checker)))
        elif not attribute:
            attribute.append(("#text", "<empty>"))
    attribute.sort()
    if depth >= DEPTH_MAX:
        return (_qname(e.tag), tuple(attribute), (("<too-deep>", (), ()),))
    children = tuple(_node(k, now, checker, depth + 1) for k in children_elements[:CHILDREN_MAX])
    if len(children_elements) > CHILDREN_MAX:
        children += (("<truncated>", (("#text", str(len(children_elements))),), ()),)
    return (_qname(e.tag), tuple(attribute), children)


def _without_time_window(node: tuple) -> tuple:
    name, attribute, children = node
    return (name, tuple(a for a in attribute if a[0] not in _TIME_WINDOW), tuple(_without_time_window(k) for k in children))


def _header(header: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for raw_name, raw_value in header.items():
        name = str(raw_name).lower()
        if name in _HEADER_NEVER or not _HEADER_NAME.fullmatch(name):
            continue
        value = ""
        w = str(raw_value)
        if name == "user-agent":
            tokens = [x for x in re.split(r"[\s;()]+", w) if _UA_TOKEN.fullmatch(x)]
            value = " ".join(tokens[:6])
        elif name == "soapaction":
            hit = _ACTION.fullmatch(w.strip().strip('"'))
            value = f"{hit.group(1)}/{hit.group(2)}" if hit else classification_text(w)
        elif name == "content-type":
            media = w.split(";")[0].strip().lower()
            value = media if _MEDIA.fullmatch(media) else classification_text(w)
        out.append((name, value))
    return tuple(sorted(out))


def skeleton(request: Request, header: Mapping[str, str], now: datetime, *, id_checker: IdChecker | None = None) -> Skeleton:
    """Pure, total, never raises. `id_checker(kind, value)` decides what is "valid"; without a checker only the form counts."""
    try:
        tree = _node(request.payload, now, id_checker, 0)
        op = request.operation if _NAME.fullmatch(request.operation or "") else "<name>"
        header_t = _header(header)
    except Exception:  # the capture must never disturb a request
        tree, op, header_t = ("<error>", (), ()), "<error>", ()
    raw = json.dumps(_without_time_window(tree), ensure_ascii=True, separators=(",", ":"))
    return Skeleton(op, tree, header_t, hashlib.sha256(raw.encode()).hexdigest()[:16])


def skeleton_as_dict(s: Skeleton) -> dict:
    def node(k: tuple) -> dict:
        return {"name": k[0], "attribute": [list(a) for a in k[1]], "children": [node(x) for x in k[2]]}

    return {"operation": s.operation, "fingerprint": s.fingerprint, "header": [list(h) for h in s.header], "tree": node(s.tree)}


# ---------------------------------------------------------------- capture storage


class ProtocolCapture:
    """Writes only for accounts with test=True and capture=True. Writing goes through the injected storage."""

    def __init__(self, directory: Path, clock: "Clock", storage: Storage) -> None:
        self._dir = Path(directory)
        self._clock = clock
        self._storage = storage
        self._last: dict[tuple[str, str], float] = {}
        self._seen: set[str] = set()

    async def capture(self, account: "Account", s: Skeleton, response_codes: Sequence[str], duration_ms: int) -> None:
        if not (getattr(account, "test", False) and getattr(account, "capture", False)):
            return
        now = self._clock.now()
        moment = datetime.fromtimestamp(now, timezone.utc)
        key = (str(account.name), s.operation)
        before = self._last.get(key)
        self._last[key] = now
        codes = [c for c in (str(c) for c in response_codes) if _VALUE.fullmatch(c)][:50]
        line = {
            "time": moment.isoformat(timespec="milliseconds"),
            "operation": s.operation,
            "fingerprint": s.fingerprint,
            "codes": codes,
            "duration_ms": int(duration_ms),
            "gap_s": None if before is None else round(now - before, 3),
        }
        self._storage.write(self._dir / f"{moment:%Y-%m-%d}.jsonl", json.dumps(line, ensure_ascii=True, separators=(",", ":")))
        if s.fingerprint not in self._seen:
            self._seen.add(s.fingerprint)
            self._storage.write(self._dir / "skeletons" / f"{s.fingerprint}.json", json.dumps(skeleton_as_dict(s), ensure_ascii=True, indent=1), one_time=True)
