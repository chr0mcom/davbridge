"""Handler contract and shared building blocks of the EWS operations. `operations/*` do not import each other."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Final

from lxml import etree

from .. import soap
from ..auth import Session
from ..caldav import Caldav, Credentials, DavObject
from ..calendar_info import CalendarInfoCache
from ..clock import Clock
from ..config import Account, CalendarConfig, Config
from ..errors import Code, EwsError
from ..event_log import EventLog
from ..identifiers import ElementRef, IdService, InvalidId
from ..locks import Locks
from ..notifier import Notifier
from ..permissions import Permissions, effective_rights, element_permissions, folder_permissions
from ..store import Store
from ..subscriptions import SubscriptionManagement

T = soap.TYPES_NS
MAX_ICS_CHAR: Final = 256 * 1024
M = soap.MESSAGES_NS


@dataclass(frozen=True, slots=True)
class Context:
    session: Session
    request: soap.Request
    config: Config
    ids: IdService
    store: Store
    caldav: Caldav
    subscriptions: SubscriptionManagement
    notifier: Notifier
    locks: Locks
    info: CalendarInfoCache
    clock: Clock
    log: EventLog
    request_id: str
    end: asyncio.Event
    from_outlook: bool = True

    @property
    def account(self) -> Account:
        return self.session.account

    @property
    def credentials(self) -> Credentials:
        return self.session.credentials


@dataclass(frozen=True, slots=True)
class Messages:
    messages: tuple[etree._Element, ...]
    codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Stream:
    chunks: AsyncIterator[bytes]


@dataclass(frozen=True, slots=True)
class Reply:
    """Operations with a different response shape (GetDelegate, GetAppManifests, ...): the handler builds the `m:...Response` itself."""

    root: etree._Element
    codes: tuple[str, ...] = ("NoError",)


Handler = Callable[[Context], Awaitable[Messages | Stream | Reply]]
OPERATIONS: dict[str, Handler] = {}


def operation(name: str) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        OPERATIONS[name] = fn
        return fn

    return decorator


# ------------------------------------------------------------------ folder model


@dataclass(frozen=True, slots=True)
class Folder:
    key: str
    tag: str
    name: str
    classification: str
    parents: str
    cal: CalendarConfig | None = None


_STATIC: Final = (
    ("msgfolderroot", "Folder", "Top of Information Store", "IPF.Note", "root"),
    ("inbox", "Folder", "Inbox", "IPF.Note", "msgfolderroot"),
    ("sentitems", "Folder", "Sent Items", "IPF.Note", "msgfolderroot"),
    ("drafts", "Folder", "Drafts", "IPF.Note", "msgfolderroot"),
    ("deleteditems", "Folder", "Deleted Items", "IPF.Note", "msgfolderroot"),
    ("outbox", "Folder", "Outbox", "IPF.Note", "msgfolderroot"),
    ("junkemail", "Folder", "Junk E-mail", "IPF.Note", "msgfolderroot"),
    ("archive", "Folder", "Archive", "IPF.Note", "msgfolderroot"),
    ("conversationhistory", "Folder", "Conversation History", "IPF.Note", "msgfolderroot"),
    ("scheduled", "Folder", "Scheduled", "IPF.Note", "msgfolderroot"),
    ("syncissues", "Folder", "Sync Issues", "IPF.Note", "msgfolderroot"),
    ("recipientcache", "ContactsFolder", "Recipient Cache", "IPF.Contact", "msgfolderroot"),
    ("contacts", "ContactsFolder", "Contacts", "IPF.Contact", "msgfolderroot"),
    ("tasks", "TasksFolder", "Tasks", "IPF.Task", "msgfolderroot"),
)
ROOT: Final = Folder("root", "Folder", "Mailbox", "IPF.Note", "root")


def folder_list(account: Account) -> list[Folder]:
    """Fixed, empty mail folders plus the configured calendars (never discovered from the CalDAV server)."""
    default = account.default_calendar()
    listing = [ROOT] + [Folder(*o) for o in _STATIC]
    listing.append(Folder(default.key, "CalendarFolder", default.display_name, "IPF.Appointment", "msgfolderroot", default))
    for cal in account.calendar:
        if cal is not default:
            listing.append(Folder(cal.key, "CalendarFolder", cal.display_name, "IPF.Appointment", default.key, cal))
    return listing


def folder_after_key(account: Account, key: str) -> Folder | None:
    return next((o for o in folder_list(account) if o.key == key), None)


def distinguished(account: Account, name: str) -> Folder | None:
    if name == "calendar":
        return folder_after_key(account, account.default_calendar().key)
    if name == "root":
        return ROOT
    return folder_after_key(account, name) if not any(k.key == name for k in account.calendar) else None


def folder_from_xml(ctx: Context, node: etree._Element) -> Folder:
    """`t:DistinguishedFolderId` or `t:FolderId`. Foreign mailbox => ErrorNonExistentMailbox, unknown => ErrorFolderNotFound."""
    local = etree.QName(node).localname
    if local == "DistinguishedFolderId":
        address = node.findtext("t:Mailbox/t:EmailAddress", None, soap.NS)
        if address and address.strip().lower() != ctx.account.address.lower():
            raise EwsError(Code.ErrorNonExistentMailbox)
        found = distinguished(ctx.account, node.get("Id", ""))
        if found is None:
            raise EwsError(Code.ErrorFolderNotFound)
        ctx.log.event("folder_access", folder=found.key, reason="distinguished", operation=ctx.request.operation, request_id=ctx.request_id)
        return found
    try:
        key = ctx.ids.read_folder_id(ctx.account.name, node.get("Id", ""))
    except InvalidId:
        raise EwsError(Code.ErrorInvalidIdMalformed) from None
    found = folder_after_key(ctx.account, key)
    if found is None:
        raise EwsError(Code.ErrorFolderNotFound)
    ctx.log.event("folder_access", folder=found.key, reason="id", operation=ctx.request.operation, request_id=ctx.request_id)
    return found


def folder_node(root: etree._Element, *paths: str) -> list[etree._Element]:
    """All folder identifiers (distinguished or FolderId) below the given parent elements, in request order."""
    result: list[etree._Element] = []
    for path in paths:
        for parents in root.findall(path, soap.NS):
            result.extend(k for k in parents if etree.QName(k).localname in ("DistinguishedFolderId", "FolderId"))
    return result


def folder_ck(ctx: Context, key: str) -> str:
    """The folder ChangeKey follows the CalDAV sync token of the calendar (as on Exchange it changes with the content).
    Outlook polls the folders about every 250 s and uses it to notice that a calendar needs to be resynchronized. Mail folders: fixed."""
    state = ctx.notifier.state(ctx.account.name, key)
    marker = state.token.encode() if state is not None and state.token else b"v1"
    return ctx.ids.folder_change_key(ctx.account.name, key, marker)


def folder_id(ctx: Context, key: str, with_change_key: bool = False) -> etree._Element:
    attr = {"Id": ctx.ids.folder_id(ctx.account.name, key)}
    if with_change_key:
        attr["ChangeKey"] = folder_ck(ctx, key)
    return soap.t("FolderId", **attr)


def parent_id(ctx: Context, key: str) -> etree._Element:
    return soap.t("ParentFolderId", Id=ctx.ids.folder_id(ctx.account.name, key), ChangeKey=folder_ck(ctx, key))


async def permissions_for(ctx: Context, folder: Folder) -> Permissions:
    if folder.cal is None:
        return Permissions(True, False, False, False, False)
    info = await ctx.info.get(ctx.credentials, folder.cal)
    return folder_permissions(folder.cal, info, ctx.config.read_only_owners)


def children(ctx: Context, key: str) -> list[Folder]:
    return [o for o in folder_list(ctx.account) if o.parents == key and o.key != key]


async def folder_xml(ctx: Context, folder: Folder) -> etree._Element:
    permissions = await permissions_for(ctx, folder)
    count = 0
    if folder.cal is not None:
        state = ctx.notifier.state(ctx.account.name, folder.cal.key)
        count = len(state.known) if state is not None else 0
    e = soap.t(folder.tag)
    e.append(folder_id(ctx, folder.key, True))
    e.append(parent_id(ctx, folder.parents))
    e.append(soap.t("FolderClass", folder.classification))
    e.append(soap.t("DisplayName", folder.name))
    e.append(soap.t("TotalCount", str(count)))
    e.append(soap.t("ChildFolderCount", str(len(children(ctx, folder.key)))))
    e.append(effective_rights(permissions, folder=True))
    return e


def hierarchy_fingerprint(ctx: Context) -> bytes:
    raw = "|".join(f"{o.key}:{o.name}:{o.parents}:{o.cal.write if o.cal else '-'}" for o in folder_list(ctx.account))
    return hashlib.sha256(raw.encode()).digest()[:8]


# ------------------------------------------------------------------ items


def shape(root: etree._Element) -> str:
    """`IdOnly` with `AdditionalProperties` means "ID plus these fields" (that is how Outlook asks); DAVbridge then returns the full set (a superset)."""
    base = root.findtext("m:ItemShape/t:BaseShape", "IdOnly", soap.NS) or "IdOnly"
    if base == "IdOnly" and len(root.findall("m:ItemShape/t:AdditionalProperties/*", soap.NS)) > 0:
        return "AllProperties"
    return base


def element_ref(ctx: Context, node: etree._Element) -> tuple[ElementRef, str | None]:
    """`t:ItemId` -> (reference, raw client ChangeKey or None). Foreign account/type/calendar => ErrorInvalidIdMalformed."""
    try:
        ref = ctx.ids.read_element_id(ctx.account.name, node.get("Id", ""))
    except InvalidId:
        raise EwsError(Code.ErrorInvalidIdMalformed) from None
    if ctx.account.calendar_after_key(ref.cal) is None:
        raise EwsError(Code.ErrorInvalidIdMalformed)
    return ref, node.get("ChangeKey")


def client_etag(ctx: Context, ref: ElementRef, change_key: str | None) -> str:
    """If-Match comes from the client's ChangeKey, never from a freshly read ETag."""
    if not change_key:
        raise EwsError(Code.ErrorChangeKeyRequiredForWriteOperations)
    try:
        return ctx.ids.read_change_key(ctx.account.name, ref, change_key)
    except InvalidId:
        raise EwsError(Code.ErrorInvalidChangeKey) from None


def item_id(ctx: Context, ref: ElementRef, etag: str | None = None) -> etree._Element:
    attr = {"Id": ctx.ids.element_id(ctx.account.name, ref)}
    if etag:
        attr["ChangeKey"] = ctx.ids.change_key(ctx.account.name, ref, etag)
    return soap.t("ItemId", **attr)


def calendar_element(ctx: Context, folder_key: str, ref: ElementRef, dav_object: DavObject, appointment, permissions: Permissions, complete: bool) -> etree._Element:
    """`t:CalendarItem` via ical.build_calendar_item (permissions only from `Permissions`, never as a blanket value). `complete=False` => only ItemId
    with ChangeKey."""
    from .. import ical

    return ical.build_calendar_item(
        appointment,
        item_id=ctx.ids.element_id(ctx.account.name, ref),
        change_key=ctx.ids.change_key(ctx.account.name, ref, dav_object.etag),
        folder_id=ctx.ids.folder_id(ctx.account.name, folder_key),
        folder_change_key=folder_ck(ctx, folder_key),
        permissions=permissions,
        mailbox_name=ctx.account.display_name,
        mailbox_address=ctx.account.address,
        features=ctx.config.features,
        only_id=not complete,
    )


async def filter_visible(ctx: Context, dav_objects: list[DavObject]) -> dict[str, tuple[DavObject, object]]:
    """One visibility filter for FindItem, GetItem and SyncFolderItems. Returns href -> (object, appointment)."""
    from .. import ical

    def parse() -> dict[str, tuple[DavObject, object]]:
        result: dict[str, tuple[DavObject, object]] = {}
        for o in dav_objects:
            if o.ics is None or len(o.ics) > MAX_ICS_CHAR:
                continue
            ok, _ = ical.representable(o.ics)
            if not ok:
                continue
            appointments = ical.read(o.ics)
            if len(appointments) == 1 and appointments[0].representable:
                result[o.href] = (o, appointments[0])
        return result

    if len(dav_objects) > 10:
        return await asyncio.to_thread(parse)
    return parse()


async def load_visible(ctx: Context, cal: CalendarConfig, hrefs: Iterable[str]) -> dict[str, tuple[DavObject, object]]:
    return await filter_visible(ctx, await ctx.caldav.get_several(ctx.credentials, cal, list(hrefs)))


def etag_hash(etag: str) -> str:
    return hashlib.sha256(etag.encode()).hexdigest()[:8]


async def remember_state(ctx: Context, kind: str, folder_key: str, ref: ElementRef, etag: str) -> None:
    await ctx.store.event(ctx.account.name, kind, folder=folder_key, element=ctx.ids.element_hash(ctx.account.name, ref), etag_hash=etag_hash(etag))


async def anchor_etag(ctx: Context, folder_key: str, ref: ElementRef, current: str) -> str | None:
    """Outlook sends no ChangeKey. If-Match then comes from the state Outlook saw last: if it does not match the
    current ETag, the item has changed on the CalDAV server since => None (conflict). Without an anchor (metadata discarded) the current ETag applies."""
    anchor = await ctx.store.last_state(ctx.account.name, folder_key, ctx.ids.element_hash(ctx.account.name, ref))
    if anchor is None or anchor == etag_hash(current):
        return current
    return None


async def deliver(ctx: Context, folder: Folder, ref: ElementRef, dav_object: DavObject, appointment, complete: bool) -> etree._Element:
    """Builds the CalendarItem and records the delivery time for the latency measurement, only for requests from Outlook (user agent)."""
    info = await permissions_for(ctx, folder)
    permissions = element_permissions(info, appointment)
    if ctx.from_outlook:
        await remember_state(ctx, "delivered", folder.key, ref, dav_object.etag)
    return calendar_element(ctx, folder.key, ref, dav_object, appointment, permissions, complete)


def import_all() -> None:
    from . import autodiscover, auxiliary, folders, items, notifications, sync  # noqa: F401


async def safe(op: str, coro: Awaitable[etree._Element], *, writing: bool = False) -> etree._Element:
    """The failure of one item becomes its error message; the other messages are preserved."""
    from ..caldav import CaldavError, CaldavKind
    from ..errors import from_caldav

    try:
        return await coro
    except EwsError as e:
        return soap.message(op, e.code, backoff_ms=e.backoff_ms)
    except CaldavError as e:
        if e.kind == CaldavKind.AUTH:
            raise
        f = from_caldav(e, writing=writing)
        return soap.message(op, f.code, backoff_ms=f.backoff_ms)
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        return soap.message(op, Code.ErrorInternalServerError)


def codes(messages: Iterable[etree._Element]) -> tuple[str, ...]:
    return tuple((n.findtext("m:ResponseCode", "", soap.NS) or "") for n in messages)


def result(op: str, messages: list[etree._Element]) -> Messages:
    return Messages(tuple(messages), codes(messages))
