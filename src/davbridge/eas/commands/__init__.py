"""Handler contract and shared building blocks of the EAS commands. `commands/*` do not import each other.

The folder list, the fingerprint and the visibility filter are deliberate duplicates of the EWS building blocks in `operations/`."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from ... import ical
from ...auth import Session
from ...caldav import Caldav, Credentials, DavObject
from ...calendar_info import CalendarInfoCache
from ...clock import Clock
from ...config import Account, CalendarConfig, Config, EasConfig
from ...event_log import EventLog
from ...identifiers import ElementRef, IdService
from ...locks import Locks
from ...permissions import Permissions, folder_permissions
from ...store import Store
from .. import recurrence, tags
from ..request import EasRequest
from ..wbxml import Node

if TYPE_CHECKING:
    from ...notifier import Notifier
    from ..devices import Devices
    from ..wakeup import Waker

MAX_ICS_CHAR: Final = 256 * 1024
ADVERTISED: Final[tuple[str, ...]] = (
    "Sync",
    "SendMail",
    "SmartForward",
    "SmartReply",
    "FolderSync",
    "FolderCreate",
    "FolderDelete",
    "FolderUpdate",
    "MoveItems",
    "GetItemEstimate",
    "MeetingResponse",
    "Search",
    "Settings",
    "Ping",
    "ItemOperations",
    "Provision",
    "ResolveRecipients",
)


@dataclass(frozen=True, slots=True)
class EasContext:
    session: Session
    request: EasRequest
    payload: Node | None
    raw_hash: bytes
    config: Config
    ids: IdService
    store: Store
    caldav: Caldav
    info: CalendarInfoCache
    locks: Locks
    notifier: "Notifier"
    waker: "Waker"
    devices: "Devices"
    clock: Clock
    log: EventLog
    request_id: str
    end: asyncio.Event
    device: str = ""
    raw: bytes = b""

    @property
    def account(self) -> Account:
        return self.session.account

    @property
    def credentials(self) -> Credentials:
        return self.session.credentials

    @property
    def eas(self) -> EasConfig:
        return self.config.eas


@dataclass(frozen=True, slots=True)
class EasResponse:
    root: Node | None
    http_status: int = 200
    codes: tuple[str, ...] = ()
    header: Mapping[str, str] = MappingProxyType({})
    count: int = 0


EasHandler = Callable[[EasContext], Awaitable[EasResponse]]
COMMANDS: dict[str, EasHandler] = {}


def command(name: str) -> Callable[[EasHandler], EasHandler]:
    def decorator(fn: EasHandler) -> EasHandler:
        COMMANDS[name] = fn
        return fn

    return decorator


def import_all() -> None:
    from . import folders, items, ping, provision, rejected, settings, sync  # noqa: F401


# ------------------------------------------------------------------ folders


@dataclass(frozen=True, slots=True)
class EasFolder:
    key: str
    name: str
    type: int
    cal: CalendarConfig | None = None


_MAIL_FOLDER: Final = (
    ("inbox", "Inbox", 2),
    ("drafts", "Drafts", 3),
    ("deleteditems", "Deleted Items", 4),
    ("sentitems", "Sent Items", 5),
    ("outbox", "Outbox", 6),
    ("tasks", "Tasks", 7),
    ("contacts", "Contacts", 9),
)


def folder_list(account: Account) -> list[EasFolder]:
    default = account.default_calendar()
    listing = [EasFolder(k, n, t) for k, n, t in _MAIL_FOLDER]
    listing.append(EasFolder(default.key, default.display_name, 8, default))
    listing.extend(EasFolder(k.key, k.display_name, 13, k) for k in account.calendar if k is not default)
    return listing


OWN_FOLDER: Final = re.compile(r"u-[0-9a-f]{10}")


def folder(account: Account, key: str | None) -> EasFolder | None:
    """Fixed folders from the configuration; `u-<hex>` are empty mail folders created by the client (type 12) that never carry content."""
    if not key:
        return None
    if OWN_FOLDER.fullmatch(key):
        return EasFolder(key, "", 12)
    return next((o for o in folder_list(account) if o.key == key), None)


def hierarchy_fingerprint(account: Account, own: Iterable[tuple[str, str, int]] = ()) -> bytes:
    raw = "|".join(f"{o.key}:{o.name}:0:{o.type}:{o.cal.write if o.cal else '-'}" for o in folder_list(account))
    raw += "|" + "|".join(f"{k}:{n}:{t}" for k, n, t in sorted(own))
    return hashlib.sha256(raw.encode()).digest()[:8]


async def permissions_for(ctx: EasContext, o: EasFolder) -> Permissions:
    if o.cal is None:
        return Permissions(True, False, False, False, False)
    info = await ctx.info.get(ctx.credentials, o.cal)
    return folder_permissions(o.cal, info, ctx.config.read_only_owners)


# ------------------------------------------------------------------ identifiers


def server_id(ctx: EasContext, folder_key: str, href: str) -> str:
    return ctx.ids.element_hash(ctx.account.name, ElementRef(folder_key, href))


def href_for(ctx: EasContext, folder_key: str, sid: str | None, candidates: Iterable[str]) -> str | None:
    if not sid or len(sid) != 12:
        return None
    for h in candidates:
        if server_id(ctx, folder_key, h) == sid:
            return h
    return None


# ------------------------------------------------------------------ visibility (one filter for Sync/Fetch/Estimate)


@dataclass(frozen=True, slots=True)
class Visible:
    dav_object: DavObject
    appointment: ical.Appointment
    is_series: bool


def check_visible(dav_objects: Iterable[DavObject]) -> dict[str, Visible]:
    result: dict[str, Visible] = {}
    for o in dav_objects:
        if o.ics is None or len(o.ics) > MAX_ICS_CHAR:
            continue
        appointments = ical.read(o.ics)
        if len(appointments) == 1 and appointments[0].representable:
            result[o.href] = Visible(o, appointments[0], False)
            continue
        if appointments and appointments[0].reason == "series":
            ok, _reason = recurrence.is_mappable(o.ics)
            if ok:
                result[o.href] = Visible(o, appointments[0], True)
    return result


async def load_visible(ctx: EasContext, cal: CalendarConfig, hrefs: list[str]) -> dict[str, Visible]:
    result: dict[str, Visible] = {}
    for i in range(0, len(hrefs), 100):
        dav_objects = await ctx.caldav.get_several(ctx.credentials, cal, hrefs[i : i + 100])
        result.update(await asyncio.to_thread(check_visible, dav_objects) if len(dav_objects) > 10 else check_visible(dav_objects))
    return result


def now(ctx: EasContext) -> datetime:
    return datetime.fromtimestamp(ctx.clock.now(), timezone.utc)


def response(root: Node | None, *codes: object, count: int = 0) -> EasResponse:
    return EasResponse(root, 200, tuple(str(c) for c in codes), count=count)


def node(qname: str, *children: Node | None, text: object | None = None) -> Node:
    return tags.node(qname, *children, text=None if text is None else str(text))


# ------------------------------------------------------------------ delta (duplicate of operations/sync.py)


async def get_delta(ctx: EasContext, cal: CalendarConfig, token: str | None):
    from ...caldav import CaldavError, CaldavKind

    try:
        return await ctx.caldav.sync(ctx.credentials, cal, token)
    except CaldavError as e:
        if e.kind != CaldavKind.TOKEN_INVALID:
            raise
        return await ctx.caldav.sync(ctx.credentials, cal, None)


def mix(known: Mapping[str, str], open: Mapping[str, str | None], delta) -> dict[str, str | None]:
    """New `open` set after a CalDAV delta: changed/new entries with ETag, deleted ones as None (only if the client knows them)."""
    new: dict[str, str | None] = dict(open)
    if delta.full:
        current = dict(delta.changed)
        for h, etag in current.items():
            if new.get(h, known.get(h)) != etag:
                new[h] = etag
        for h in (set(known) | {x for x, v in new.items() if v is not None}) - set(current):
            new[h] = None
    else:
        for h, etag in delta.changed.items():
            new[h] = etag
        for h in delta.deleted:
            new[h] = None
    for h in list(new):
        if new[h] is None and h not in known:
            del new[h]
        elif new[h] is not None and known.get(h) == new[h]:
            del new[h]
    return new


def body_options(options: Mapping) -> tuple[list[int], int | None]:
    body = options.get("body") or []
    types = [int(b.get("type", 1)) for b in body if isinstance(b, Mapping)] or [1]
    trunc = next((b.get("trunc") for b in body if isinstance(b, Mapping) and b.get("trunc") is not None), None)
    return types, (int(trunc) if trunc is not None else None)


def read_options(c: Node) -> dict | None:
    """AirSync:Options -> {"body": [{"type", "trunc"}], "filter": n, "conflict": n}; None if no options were sent."""
    opt = tags.find(c, "AirSync:Options")
    if opt is None:
        return None
    body = []
    for bp in tags.find_all(opt, "AirSyncBase:BodyPreference"):
        type = tags.text_from(bp, "AirSyncBase:Type")
        trunc = tags.text_from(bp, "AirSyncBase:TruncationSize")
        if type and type.isdigit():
            body.append({"type": int(type), "trunc": int(trunc) if trunc and trunc.isdigit() else None})
    res: dict = {"body": body[:4]}
    f = tags.text_from(opt, "AirSync:FilterType")
    if f and f.isdigit():
        res["filter"] = int(f)
    k = tags.text_from(opt, "AirSync:Conflict")
    if k and k.isdigit():
        res["conflict"] = int(k)
    return res
