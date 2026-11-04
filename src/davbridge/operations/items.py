"""FindItem, GetItem, CreateItem, UpdateItem, DeleteItem (§3.4–§3.6, C17–C25)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from lxml import etree

from .. import ical, soap, timezones
from ..caldav import CaldavError, CaldavKind
from ..errors import Code, EwsError
from ..ical import ContextZone, FieldRejected
from ..identifiers import ElementRef
from ..permissions import element_permissions
from . import (
    Context,
    Messages,
    anchor_etag,
    calendar_element,
    client_etag,
    deliver,
    element_ref,
    filter_visible,
    folder_after_key,
    folder_from_xml,
    folder_node,
    load_visible,
    operation,
    permissions_for,
    remember_state,
    result,
    safe,
    shape,
)

NS = soap.NS


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _has_uid(ics: str, uid: str) -> bool:
    try:
        return any(t.uid == uid for t in ical.read(ics))
    except ValueError:
        return False


def _start(appointment) -> float:
    try:
        return appointment.start.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def _context_zone(ctx: Context) -> ContextZone:
    if not ctx.request.timezone:
        return ContextZone()
    try:
        return ContextZone(iana=timezones.iana_for_windows(ctx.request.timezone))
    except KeyError:
        raise EwsError(Code.ErrorTimeZone) from None


def _rejected_create(e: FieldRejected) -> EwsError:
    return EwsError(Code.ErrorInvalidOperation if e.reason == "attendees" else Code.ErrorInvalidRequest)


def _rejected_update(e: FieldRejected) -> EwsError:
    return EwsError(Code.ErrorInvalidOperation if e.reason == "attendees" else Code.ErrorUnsupportedPathForSet)


def _convert(fn, *args):
    try:
        return fn(*args)
    except KeyError:
        raise EwsError(Code.ErrorTimeZone) from None
    except ValueError as e:
        raise EwsError(Code.ErrorCalendarEndDateIsEarlierThanStartDate if str(e).startswith("end is before") else Code.ErrorInvalidPropertySet) from None


# ------------------------------------------------------------------ reading


@operation("FindItem")
async def find_item(ctx: Context) -> Messages:
    k = ctx.request.payload
    complete = shape(k) != "IdOnly"
    view = k.find("m:CalendarView", NS)
    page = k.find("m:IndexedPageItemView", NS)
    limit = None
    offset = 0
    from_end = False
    if view is not None and view.get("MaxEntriesReturned", "").isdigit():
        limit = int(view.get("MaxEntriesReturned"))
    elif page is not None:
        if page.get("MaxEntriesReturned", "").isdigit():
            limit = int(page.get("MaxEntriesReturned"))
        offset = int(page.get("Offset")) if page.get("Offset", "").isdigit() else 0
        from_end = page.get("BasePoint", "Beginning") == "End"
    if limit is not None and limit < 1:
        raise EwsError(Code.ErrorInvalidRequest)

    async def one(node):
        folder = folder_from_xml(ctx, node)
        items = soap.t("Items")
        found: list = []
        if folder.cal is not None:
            since, until = (_time(view.get("StartDate")), _time(view.get("EndDate"))) if view is not None else (None, None)
            if since and until:
                visible = await filter_visible(ctx, await ctx.caldav.time_range(ctx.credentials, folder.cal, since, until))
            else:
                delta = await ctx.caldav.sync(ctx.credentials, folder.cal, None)
                visible = await load_visible(ctx, folder.cal, sorted(delta.changed))
            found = [visible[h] for h in sorted(visible, key=lambda h: (_start(visible[h][1]), h))]
        total = len(found)
        last = True
        next = 0
        if page is not None and view is None:
            start = max(0, total - offset) if from_end else min(offset, total)
            found = found[start:]
            next = start
        if limit is not None and len(found) > limit:
            found, last = found[:limit], False
        next += len(found)
        for dav_object, appointment in found:
            items.append(await deliver(ctx, folder, ElementRef(folder.cal.key, dav_object.href), dav_object, appointment, complete))
        root = soap.m("RootFolder", TotalItemsInView=str(total), IncludesLastItemInRange="true" if last else "false")
        if page is not None and view is None:
            root.set("IndexedPagingOffset", str(next))
        root.append(items)
        return soap.message("FindItem", Code.NoError, root)

    node = folder_node(k, "m:ParentFolderIds")
    if not node:
        return result("FindItem", [soap.message("FindItem", Code.ErrorFolderNotFound)])
    return result("FindItem", [await safe("FindItem", one(n)) for n in node])


@operation("GetItem")
async def get_item(ctx: Context) -> Messages:
    complete = shape(ctx.request.payload) != "IdOnly"
    node = ctx.request.payload.findall("m:ItemIds/t:ItemId", NS)
    refs: list[ElementRef | EwsError] = []
    for n in node:
        try:
            refs.append(element_ref(ctx, n)[0])
        except EwsError as e:
            refs.append(e)
    per_calendar: dict[str, set[str]] = {}
    for r in refs:
        if isinstance(r, ElementRef):
            per_calendar.setdefault(r.cal, set()).add(r.href)
    loaded: dict[str, dict] = {}
    error_per_calendar: dict[str, Exception] = {}
    for key, hrefs in per_calendar.items():
        cal = ctx.account.calendar_after_key(key)
        try:
            loaded[key] = await load_visible(ctx, cal, sorted(hrefs))
        except Exception as e:  # noqa: BLE001
            error_per_calendar[key] = e
    messages = []
    for r in refs:

        async def one(r=r):
            if isinstance(r, EwsError):
                raise r
            if r.cal in error_per_calendar:
                raise error_per_calendar[r.cal]
            hit = loaded.get(r.cal, {}).get(r.href)
            if hit is None:
                raise EwsError(Code.ErrorItemNotFound)
            folder = folder_after_key(ctx.account, r.cal)
            items = soap.m("Items")
            items.append(await deliver(ctx, folder, r, hit[0], hit[1], complete))
            return soap.message("GetItem", Code.NoError, items)

        messages.append(await safe("GetItem", one()))
    if not messages:
        messages.append(soap.message("GetItem", Code.ErrorItemNotFound))
    return result("GetItem", messages)


# ------------------------------------------------------------------ writing


def _now(ctx: Context) -> datetime:
    return datetime.fromtimestamp(ctx.clock.now(), timezone.utc)


async def _response_item(ctx: Context, folder, ref: ElementRef, complete: bool = True, etag: str | None = None):
    """`etag` is the ETag of our own write response: if someone else wrote in the meantime, Outlook gets the ChangeKey of its version and thus the
    conflict on the next update."""
    cal = folder.cal
    dav_object = await ctx.caldav.get(ctx.credentials, cal, ref.href)
    if dav_object is None or dav_object.ics is None:
        raise EwsError(Code.ErrorItemNotFound)
    if etag and etag != dav_object.etag:
        dav_object = replace(dav_object, etag=etag)
    appointments = ical.read(dav_object.ics)
    if len(appointments) != 1:
        raise EwsError(Code.ErrorItemNotFound)
    info = await permissions_for(ctx, folder)
    permissions = element_permissions(info, appointments[0])
    await remember_state(ctx, "written", folder.key, ref, dav_object.etag)
    return calendar_element(ctx, folder.key, ref, dav_object, appointments[0], permissions, complete)


@operation("CreateItem")
async def create_item(ctx: Context) -> Messages:
    k = ctx.request.payload
    target = folder_node(k, "m:SavedItemFolderId")
    node = list(k.find("m:Items", NS) if k.find("m:Items", NS) is not None else [])

    async def one(item):
        local = etree.QName(item).localname
        if local != "CalendarItem":
            raise EwsError(Code.ErrorAccessDenied if local in ("Message", "MeetingRequest") else Code.ErrorInvalidRequest)
        folder = folder_from_xml(ctx, target[0]) if target else folder_after_key(ctx.account, ctx.account.default_calendar().key)
        if folder.cal is None:
            raise EwsError(Code.ErrorAccessDenied)
        permissions = await permissions_for(ctx, folder)
        if not permissions.create:
            raise EwsError(Code.ErrorAccessDenied)
        try:
            uid, fields = _convert(ical.read_creation, item, ctx.config.features, _context_zone(ctx))
        except FieldRejected as e:
            raise _rejected_create(e) from None
        dedup = None
        if uid:
            href = ical.href_for_uid(uid)
        else:
            dedup = ctx.ids.dedup_hash(ctx.account.name, folder.key, ical.canonical(fields))
            uid = str(uuid.uuid4())
            href = ical.href_for_uid(uid)
        ref = ElementRef(folder.key, href)
        async with ctx.locks.creation(ctx.account.name, dedup or href):
            if dedup is not None:
                present = await ctx.store.dedup_get(ctx.account.name, dedup)
                if present and present[0] == folder.key:
                    href = present[1]
                    uid = href.removesuffix(".ics")
                    ref = ElementRef(folder.key, href)
                    if await ctx.caldav.get(ctx.credentials, folder.cal, href) is not None:
                        items = soap.m("Items")
                        items.append(await _response_item(ctx, folder, ref))
                        return soap.message("CreateItem", Code.NoError, items)
                else:
                    await ctx.store.dedup_remember(ctx.account.name, dedup, folder.key, href)
            ics = _convert(ical.new, uid, fields, _now(ctx))
            new_etag = None
            try:
                new_etag = await ctx.caldav.create(ctx.credentials, folder.cal, href, ics)
            except CaldavError as e:
                if e.kind == CaldavKind.UID_CONFLICT:
                    hit = await ctx.caldav.search_uid(ctx.credentials, folder.cal, uid)
                    if hit is None:
                        raise EwsError(Code.ErrorItemSave) from None
                    ref = ElementRef(folder.key, hit.href)
                elif e.kind == CaldavKind.CONFLICT:
                    present = await ctx.caldav.get(ctx.credentials, folder.cal, href)
                    if present is None or present.ics is None or not _has_uid(present.ics, uid):
                        raise EwsError(Code.ErrorItemSave) from None
                else:
                    raise
        items = soap.m("Items")
        items.append(await _response_item(ctx, folder, ref, etag=new_etag))
        return soap.message("CreateItem", Code.NoError, items)

    if not node:
        return result("CreateItem", [soap.message("CreateItem", Code.ErrorInvalidRequest)])
    return result("CreateItem", [await safe("CreateItem", one(i), writing=True) for i in node])


@operation("UpdateItem")
async def update_item(ctx: Context) -> Messages:
    changes_node = ctx.request.payload.findall("m:ItemChanges/t:ItemChange", NS)

    async def one(change):
        id_node = change.find("t:ItemId", NS)
        if id_node is None:
            raise EwsError(Code.ErrorInvalidRequest)
        ref, raw_key = element_ref(ctx, id_node)
        without_changekey = not raw_key
        etag = None if without_changekey else client_etag(ctx, ref, raw_key)
        folder = folder_after_key(ctx.account, ref.cal)
        try:
            fields = _convert(ical.read_changes, change, ctx.config.features, _context_zone(ctx))
        except FieldRejected as e:
            raise _rejected_update(e) from None
        folder_right = await permissions_for(ctx, folder)
        if not folder_right.change:
            raise EwsError(Code.ErrorAccessDenied)
        async with ctx.locks.element(ctx.account.name, ref.cal, ref.href):
            dav_object = await ctx.caldav.get(ctx.credentials, folder.cal, ref.href)
            if dav_object is None or dav_object.ics is None:
                raise EwsError(Code.ErrorItemNotFound)
            if not fields:
                items = soap.m("Items")
                items.append(await _response_item(ctx, folder, ref, etag=dav_object.etag))
                conflicts = soap.m("ConflictResults")
                conflicts.append(soap.t("Count", "0"))
                return soap.message("UpdateItem", Code.NoError, items, conflicts)
            if without_changekey:
                etag = await anchor_etag(ctx, folder.key, ref, dav_object.etag)
            if dav_object.etag != etag:
                if ical.already_applied(dav_object.ics, fields):
                    items = soap.m("Items")
                    items.append(await _response_item(ctx, folder, ref))
                    conflicts = soap.m("ConflictResults")
                    conflicts.append(soap.t("Count", "0"))
                    return soap.message("UpdateItem", Code.NoError, items, conflicts)
                raise EwsError(Code.ErrorIrresolvableConflict)
            appointments = ical.read(dav_object.ics)
            if len(appointments) != 1 or not appointments[0].representable:
                raise EwsError(Code.ErrorItemNotFound)
            if not element_permissions(folder_right, appointments[0]).change:
                raise EwsError(Code.ErrorAccessDenied)
            new = _convert(ical.patch, dav_object.ics, fields, _now(ctx))
            try:
                new_etag = await ctx.caldav.change(ctx.credentials, folder.cal, ref.href, new, etag)
            except CaldavError as e:
                if e.kind == CaldavKind.CONFLICT:
                    raise EwsError(Code.ErrorIrresolvableConflict) from None
                raise
        items = soap.m("Items")
        items.append(await _response_item(ctx, folder, ref, etag=new_etag))
        conflicts = soap.m("ConflictResults")
        conflicts.append(soap.t("Count", "0"))
        return soap.message("UpdateItem", Code.NoError, items, conflicts)

    if not changes_node:
        return result("UpdateItem", [soap.message("UpdateItem", Code.ErrorInvalidRequest)])
    return result("UpdateItem", [await safe("UpdateItem", one(c), writing=True) for c in changes_node])


@operation("DeleteItem")
async def delete_item(ctx: Context) -> Messages:
    node = ctx.request.payload.findall("m:ItemIds/t:ItemId", NS)

    async def one(n):
        ref, raw_key = element_ref(ctx, n)
        etag = client_etag(ctx, ref, raw_key) if raw_key else None
        folder = folder_after_key(ctx.account, ref.cal)
        folder_right = await permissions_for(ctx, folder)
        if not folder_right.delete:
            raise EwsError(Code.ErrorAccessDenied)
        async with ctx.locks.element(ctx.account.name, ref.cal, ref.href):
            dav_object = await ctx.caldav.get(ctx.credentials, folder.cal, ref.href)
            if dav_object is None:
                return soap.message("DeleteItem", Code.NoError)
            if dav_object.ics is not None:
                appointments = ical.read(dav_object.ics)
                if len(appointments) != 1 or not element_permissions(folder_right, appointments[0]).delete:
                    raise EwsError(Code.ErrorAccessDenied)
            if etag is None:
                etag = await anchor_etag(ctx, folder.key, ref, dav_object.etag)
                if etag is None:
                    raise EwsError(Code.ErrorIrresolvableConflict)
            if dav_object.etag != etag:
                raise EwsError(Code.ErrorIrresolvableConflict)
            try:
                await ctx.caldav.delete(ctx.credentials, folder.cal, ref.href, etag or dav_object.etag)
            except CaldavError as e:
                if e.kind != CaldavKind.CONFLICT:
                    raise
                if await ctx.caldav.get(ctx.credentials, folder.cal, ref.href) is None:
                    return soap.message("DeleteItem", Code.NoError)
                raise EwsError(Code.ErrorIrresolvableConflict) from None
        return soap.message("DeleteItem", Code.NoError)

    if not node:
        return result("DeleteItem", [soap.message("DeleteItem", Code.ErrorInvalidRequest)])
    return result("DeleteItem", [await safe("DeleteItem", one(n), writing=True) for n in node])
