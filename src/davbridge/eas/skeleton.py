"""Structure skeleton of an EAS request/response for the capture file. Allowlist: text only for enumeration/number tags."""

from __future__ import annotations

import re
from typing import Final

from . import tags
from .wbxml import Node

TEXT_ALLOWED: Final = frozenset(
    {
        "AirSync:Status",
        "AirSync:Class",
        "AirSync:FilterType",
        "AirSync:Conflict",
        "AirSync:WindowSize",
        "AirSync:GetChanges",
        "AirSync:DeletesAsMoves",
        "AirSync:HeartbeatInterval",
        "AirSync:Wait",
        "AirSync:MIMESupport",
        "AirSync:MaxItems",
        "AirSync:MoreAvailable",
        "AirSyncBase:Type",
        "AirSyncBase:TruncationSize",
        "AirSyncBase:AllOrNone",
        "AirSyncBase:Truncated",
        "AirSyncBase:NativeBodyType",
        "Calendar:AllDayEvent",
        "Calendar:BusyStatus",
        "Calendar:Sensitivity",
        "Calendar:MeetingStatus",
        "Calendar:Type",
        "Calendar:Interval",
        "Calendar:Reminder",
        "Provision:PolicyType",
        "Provision:Status",
        "Settings:OofState",
        "Settings:Status",
        "Ping:Status",
        "Ping:HeartbeatInterval",
        "Ping:Class",
        "Ping:MaxFolders",
        "FolderHierarchy:Status",
        "FolderHierarchy:Type",
        "FolderHierarchy:Count",
        "GetItemEstimate:Status",
        "GetItemEstimate:Estimate",
        "ItemOperations:Status",
        "Settings:BodyType",
        "Settings:EnableOutboundSMS",
    }
)
_VALUE: Final = re.compile(r"[A-Za-z0-9.\-]{1,24}")
MAX_ENTRIES: Final = 400


def skeleton(root: Node) -> list[str]:
    """Path list ``Page:Name[=Value]`` in document order; consecutive duplicates merged, at most 400."""
    out: list[str] = []
    work: list[tuple[Node, str]] = [(root, "")]
    while work and len(out) < MAX_ENTRIES:
        k, before = work.pop()
        name = tags.display(k)
        path = f"{before}/{name}" if before else name
        entry = path
        if k.text is not None and name in TEXT_ALLOWED and _VALUE.fullmatch(k.text.strip() or "-"):
            entry = f"{path}={k.text.strip()}"
        elif k.text is not None or k.data is not None:
            entry = f"{path}=…"
        if not out or out[-1] != entry:
            out.append(entry)
        work.extend((kind, path) for kind in reversed(k.children))
    return out
