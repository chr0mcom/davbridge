"""Sync and GetItemEstimate. State per (account, device, folder) with a counter, CAS and a retry plan.

Response order in `Collection`: SyncKey, CollectionId, Status, Responses, Commands, MoreAvailable (`_SEQUENCE`, as in the example of the specification).
Nothing to do => HTTP 200 with an empty body; every response with a body carries a new key. A retry = same key k-1 and
the same command content, window/options may change. Missing, non-ghosted fields in a Change are deleted (only for a full image).
The retry plan only holds references (hrefs, ETags, status numbers, ClientId), never appointment content."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Final

from ... import ical
from ...caldav import CaldavError, CaldavKind
from ...permissions import Permissions, element_permissions
from ...store import EasSync
from .. import calendar, tags, wbxml
from ..wbxml import Node
from . import (
    EasContext,
    EasFolder,
    EasResponse,
    Visible,
    body_options,
    command,
    folder,
    get_delta,
    href_for,
    load_visible,
    mix,
    node,
    now,
    permissions_for,
    read_options,
    response,
    server_id,
)

MAX_COMMANDS: Final = 100
MAX_COLLECTIONS: Final = 30
_SEQUENCE: Final = ("AirSync:SyncKey", "AirSync:CollectionId", "AirSync:Status", "AirSync:Responses", "AirSync:Commands", "AirSync:MoreAvailable")
_UID_NS: Final = uuid.UUID("5b0e7c1e-6a43-4f0e-9f43-2d6c0a9d4e11")


@dataclass(slots=True)
class _Run:
    ctx: EasContext
    o: EasFolder
    known: dict[str, str]
    open: dict[str, str | None]
    responses: list[dict] = field(default_factory=list)
    enforce: dict[str, str] = field(default_factory=dict)
    written: int = 0
    not_ghosted: frozenset[str] = frozenset()
    truncation: int | None = None
    conflicts: dict[str, list] = field(default_factory=dict)


def _hash_fields(fields) -> str:
    return hashlib.sha256(ical.canonical(fields)).hexdigest()[:8]


def _remember_conflict(run: "_Run", sid: str, value: str) -> None:
    old = run.conflicts.get(sid)
    n = old[1] + 1 if old and old[0] == value else 1
    run.conflicts.pop(sid, None)
    run.conflicts[sid] = [value, n]
    while len(run.conflicts) > 20:
        run.conflicts.pop(next(iter(run.conflicts)))


def _collection(sync_key: str, cid: str, status: int, responses: list[Node] | None = None, more: bool = False, commands: list[Node] | None = None) -> Node:
    parts = {
        "AirSync:SyncKey": node("AirSync:SyncKey", text=sync_key),
        "AirSync:CollectionId": node("AirSync:CollectionId", text=cid),
        "AirSync:Status": node("AirSync:Status", text=status),
        "AirSync:Responses": node("AirSync:Responses", *responses) if responses else None,
        "AirSync:MoreAvailable": node("AirSync:MoreAvailable") if more else None,
        "AirSync:Commands": node("AirSync:Commands", *commands) if commands else None,
    }
    return node("AirSync:Collection", *(parts[n] for n in _SEQUENCE))


def _sync_status(status: int) -> EasResponse:
    return response(node("AirSync:Sync", node("AirSync:Status", text=status)), status)


def _application_data(ctx: EasContext, s: Visible, options) -> Node | None:
    types, trunc = body_options(options)
    return calendar.appointment_to_application_data(
        s.appointment, s.dav_object.ics or "", body_types=types, trunc=trunc, body_max=ctx.eas.body_max_chars, account_name=ctx.account.display_name, account_address=ctx.account.address
    )


def _may_change(folder_right: Permissions, s: Visible | None, ctx: EasContext) -> bool:
    return s is not None and not s.is_series and element_permissions(folder_right, s.appointment).change


async def _visible_one(ctx: EasContext, o: EasFolder, href: str) -> tuple[object | None, Visible | None]:
    dav_object = await ctx.caldav.get(ctx.credentials, o.cal, href)
    if dav_object is None:
        return None, None
    from . import check_visible

    return dav_object, check_visible([dav_object]).get(href)


# ------------------------------------------------------------------ Client-Befehle (E24)


async def _add(run: _Run, cmd: Node, right: Permissions) -> None:
    ctx, o = run.ctx, run.o
    cid = tags.text_from(cmd, "AirSync:ClientId")
    data = tags.find(cmd, "AirSync:ApplicationData")
    if not cid or len(cid) > 64 or data is None:
        run.responses.append({"kind": "add", "cid": (cid or "")[:64], "href": None, "status": 6})
        return
    if not right.create or o.cal is None:
        ctx.log.event("change_rejected", reason="read-only", folder=o.key, request_id=ctx.request_id)
        run.responses.append({"kind": "add", "cid": cid, "href": None, "status": 6})
        return
    try:
        fields, ignored = await asyncio.to_thread(calendar.application_data_to_changes, data, new=True)
    except calendar.Rejected as e:
        ctx.log.event("change_rejected", reason=e.reason, folder=o.key, request_id=ctx.request_id)
        run.responses.append({"kind": "add", "cid": cid, "href": None, "status": 6})
        return
    for name in ignored[:20]:
        ctx.log.event("eas_field_ignored", field=name, request_id=ctx.request_id)
    try:
        uid = calendar.application_data_uid(data) or str(uuid.uuid5(_UID_NS, f"{ctx.account.name}\x00{o.key}\x00{cid}"))
    except calendar.Rejected:
        run.responses.append({"kind": "add", "cid": cid, "href": None, "status": 6})
        return
    href = ical.href_for_uid(uid)
    dedup = ctx.ids.dedup_hash(ctx.account.name, o.key, b"eas\x00" + cid.encode("utf-8"))
    async with ctx.locks.creation(ctx.account.name, dedup):
        present = await ctx.store.dedup_get(ctx.account.name, dedup)
        if present and present[0] == o.key:
            dav_object = await ctx.caldav.get(ctx.credentials, o.cal, present[1])
            if dav_object is not None:
                run.known[dav_object.href] = dav_object.etag
                run.open.pop(dav_object.href, None)
                run.responses.append({"kind": "add", "cid": cid, "href": dav_object.href, "status": 1})
                return
        await ctx.store.dedup_remember(ctx.account.name, dedup, o.key, href)
        try:
            ics = ical.new(uid, fields, now(ctx))
        except (ValueError, KeyError, TypeError):
            run.responses.append({"kind": "add", "cid": cid, "href": None, "status": 6})
            return
        etag: str | None = None
        try:
            etag = await ctx.caldav.create(ctx.credentials, o.cal, href, ics)
        except CaldavError as e:
            if e.kind == CaldavKind.UID_CONFLICT:
                hit = await ctx.caldav.search_uid(ctx.credentials, o.cal, uid)
                if hit is None:
                    run.responses.append({"kind": "add", "cid": cid, "href": None, "status": 6})
                    return
                href, etag = hit.href, hit.etag
            elif e.kind == CaldavKind.CONFLICT:
                dav_object = await ctx.caldav.get(ctx.credentials, o.cal, href)
                if dav_object is None or dav_object.ics is None or not any(t.uid == uid for t in ical.read(dav_object.ics)):
                    run.responses.append({"kind": "add", "cid": cid, "href": None, "status": 6})
                    return
                etag = dav_object.etag
            elif e.kind in (CaldavKind.FORBIDDEN, CaldavKind.QUOTA):
                run.responses.append({"kind": "add", "cid": cid, "href": None, "status": 6})
                return
            else:
                raise
        if etag is None:
            dav_object = await ctx.caldav.get(ctx.credentials, o.cal, href)
            etag = dav_object.etag if dav_object is not None else ""
        await ctx.store.dedup_remember(ctx.account.name, dedup, o.key, href)
    run.known[href] = etag
    run.open.pop(href, None)
    run.written += 1
    run.responses.append({"kind": "add", "cid": cid, "href": href, "status": 1})


async def _change(run: _Run, cmd: Node, right: Permissions) -> None:
    ctx, o = run.ctx, run.o
    sid = tags.text_from(cmd, "AirSync:ServerId")
    href = href_for(ctx, o.key, sid, list(run.known) + list(run.open))
    if href is None or o.cal is None:
        run.responses.append({"kind": "change", "sid": (sid or "")[:12], "href": None, "status": 8})
        return
    data = tags.find(cmd, "AirSync:ApplicationData") or node("AirSync:ApplicationData")
    async with ctx.locks.element(ctx.account.name, o.key, href):
        dav_object, s = await _visible_one(ctx, o, href)
        if dav_object is None:
            run.responses.append({"kind": "change", "sid": sid, "href": href, "status": 8})
            if href in run.known:
                run.open[href] = None
            return
        try:
            fields, ignored = await asyncio.to_thread(
                calendar.application_data_to_changes, data, new=False, current=s.appointment if s is not None and not s.is_series else None, not_ghosted=run.not_ghosted, truncation=run.truncation
            )
        except calendar.Rejected as e:
            ctx.log.event("change_rejected", reason=e.reason, folder=o.key, request_id=ctx.request_id)
            await _back(run, href, "change", dav_object.etag)
            return
        for name in ignored[:20]:
            ctx.log.event("eas_field_ignored", field=name, request_id=ctx.request_id)
        for name in calendar.omitted_fields(data)[:20]:
            ctx.log.event("eas_change_skipped", field=name, request_id=ctx.request_id)
        if not right.change or not _may_change(right, s, ctx):
            reason = "series" if s is not None and s.is_series else ("read-only" if not right.change else "attendees")
            ctx.log.event("change_rejected", reason=reason, folder=o.key, request_id=ctx.request_id)
            await _back(run, href, "change", dav_object.etag)
            return
        sid_k = server_id(ctx, o.key, href)
        h = _hash_fields(fields) if fields else ""
        if h and run.conflicts.get(sid_k, [None])[0] == h:
            ctx.log.event("eas_conflict", folder=o.key, reason="repeated", request_id=ctx.request_id)
            _remember_conflict(run, sid_k, h)
            await _back(run, href, "change", dav_object.etag)
            return
        if dav_object.etag != run.known.get(href):
            if dav_object.ics and ical.already_applied(dav_object.ics, fields):
                run.known[href] = dav_object.etag
                run.open.pop(href, None)
                return
            ctx.log.event("eas_conflict", folder=o.key, request_id=ctx.request_id)
            if h:
                _remember_conflict(run, sid_k, h)
            await _back(run, href, "change", dav_object.etag)
            return
        if not fields or (dav_object.ics and ical.already_applied(dav_object.ics, fields)):
            return
        if any(p in (dav_object.ics or "") for p in ctx.config.protected_property_prefixes) and any(f.field_uri not in ctx.config.protected_writable_uris for f in fields):
            ctx.log.event("change_rejected", reason="protected-property", folder=o.key, request_id=ctx.request_id)
            await _back(run, href, "change", dav_object.etag)
            return
        try:
            new_ics = ical.patch(dav_object.ics or "", fields, now(ctx))
        except (ValueError, KeyError, TypeError):
            await _back(run, href, "change", dav_object.etag)
            return
        try:
            etag = await ctx.caldav.change(ctx.credentials, o.cal, href, new_ics, dav_object.etag)
        except CaldavError as e:
            if e.kind in (CaldavKind.CONFLICT, CaldavKind.FORBIDDEN, CaldavKind.UID_CONFLICT):
                ctx.log.event("eas_conflict", folder=o.key, request_id=ctx.request_id)
                _remember_conflict(run, sid_k, h)
                await _back(run, href, "change")
                return
            raise
        if etag is None:
            new_object = await ctx.caldav.get(ctx.credentials, o.cal, href)
            etag = new_object.etag if new_object is not None else ""
    run.known[href] = etag
    run.open.pop(href, None)
    run.conflicts.pop(server_id(ctx, o.key, href), None)
    run.written += 1


async def _delete(run: _Run, cmd: Node, right: Permissions) -> None:
    ctx, o = run.ctx, run.o
    sid = tags.text_from(cmd, "AirSync:ServerId")
    href = href_for(ctx, o.key, sid, list(run.known) + list(run.open))
    if href is None or o.cal is None:
        run.responses.append({"kind": "delete", "sid": (sid or "")[:12], "href": None, "status": 8})
        return
    async with ctx.locks.element(ctx.account.name, o.key, href):
        dav_object, s = await _visible_one(ctx, o, href)
        if dav_object is None:
            run.known.pop(href, None)
            run.open.pop(href, None)
            return
        may = right.delete and s is not None and not s.is_series and element_permissions(right, s.appointment).delete
        throttled = may and not ctx.devices.delete_allowed(ctx.account.name, len(run.known), ctx.clock.now(), ctx.eas.delete_brake_minimum)
        sid_k = server_id(ctx, o.key, href)
        old = run.conflicts.get(sid_k)
        repeated = may and bool(old) and old[0] == "del" and old[1] < 2  # rejected twice, only the third identical wish applies (loop breaker)
        if not may or throttled or repeated or dav_object.etag != run.known.get(href):
            reason = "delete-brake" if throttled else ("conflict" if may else ("series" if s is not None and s.is_series else "read-only"))
            ctx.log.event("change_rejected", reason=reason, folder=o.key, request_id=ctx.request_id)
            if may and not throttled:
                _remember_conflict(run, sid_k, "del")
            await _back(run, href, "delete", dav_object.etag)
            return
        try:
            await ctx.caldav.delete(ctx.credentials, o.cal, href, dav_object.etag)
            ctx.devices.remember_deletion(ctx.account.name, ctx.clock.now())
        except CaldavError as e:
            if e.kind != CaldavKind.CONFLICT:
                raise
            if await ctx.caldav.get(ctx.credentials, o.cal, href) is not None:
                await _back(run, href, "delete")
                return
    run.known.pop(href, None)
    run.open.pop(href, None)
    run.conflicts.pop(sid_k, None)
    run.written += 1


async def _back(run: _Run, href: str, kind: str, etag: str | None = None) -> None:
    """CalDAV wins. Change => status 7 + server version as Commands/Change; a rejected Delete => the item comes back as Add."""
    if etag is None:
        dav_object = await run.ctx.caldav.get(run.ctx.credentials, run.o.cal, href)
        if dav_object is None:
            if href in run.known:
                run.open[href] = None
            return
        etag = dav_object.etag
    if kind == "change":
        run.responses.append({"kind": "change", "sid": server_id(run.ctx, run.o.key, href), "href": href, "status": 7})
    else:
        run.responses.append({"kind": "delete", "sid": server_id(run.ctx, run.o.key, href), "href": href, "status": 7})
        run.known.pop(href, None)
    run.enforce[href] = etag


async def _fetch(run: _Run, cmd: Node) -> None:
    sid = tags.text_from(cmd, "AirSync:ServerId")
    href = href_for(run.ctx, run.o.key, sid, list(run.known) + list(run.open))
    run.responses.append({"kind": "fetch", "sid": (sid or "")[:12], "href": href, "status": 1 if href else 8})


# ------------------------------------------------------------------ response building


def _without_cid(responses: list[dict]) -> list[dict]:
    """The ClientId is never stored in SQLite; the plan only remembers the position of the command (`i`), the retry reads it from the request."""
    return [{k: v for k, v in a.items() if k != "cid"} for a in responses]


async def _responses(ctx: EasContext, o: EasFolder, responses: list[dict], options, cids: list[str] | None = None) -> list[Node]:
    out: list[Node] = []
    for a in responses:
        if a["kind"] == "add":
            cid = a.get("cid")
            if cid is None:
                i = a.get("i")
                cid = cids[i] if cids is not None and i is not None and i < len(cids) else ""
            out.append(
                node(
                    "AirSync:Add", node("AirSync:ClientId", text=cid), node("AirSync:ServerId", text=server_id(ctx, o.key, a["href"])) if a["href"] else None, node("AirSync:Status", text=a["status"])
                )
            )
        elif a["kind"] in ("change", "delete"):
            out.append(node("AirSync:Change" if a["kind"] == "change" else "AirSync:Delete", node("AirSync:ServerId", text=a["sid"]), node("AirSync:Status", text=a["status"])))
        elif a["kind"] == "fetch":
            data = None
            status = a["status"]
            if a["href"] and o.cal is not None:
                s = (await load_visible(ctx, o.cal, [a["href"]])).get(a["href"])
                data = _application_data(ctx, s, options) if s else None
                status = 1 if data is not None else 8
            out.append(node("AirSync:Fetch", node("AirSync:ServerId", text=a["sid"]), node("AirSync:Status", text=status), data))
    return out


async def _deliver(ctx: EasContext, o: EasFolder, hrefs: list[str], options, open: dict, known: dict, enforce: dict, budget: int) -> tuple[list[Node], dict, bool]:
    """Items from `open` in the order of `hrefs` as Commands; advances known/open. Returns (commands, plan part, truncated)."""
    plan = {"add": [], "change": [], "delete": [], "etags": {}}
    visible = await load_visible(ctx, o.cal, [h for h in hrefs if open.get(h) is not None]) if o.cal is not None else {}
    data = await asyncio.to_thread(lambda: {h: _application_data(ctx, s, options) for h, s in visible.items()})
    commands: list[Node] = []
    size = 0
    for h in hrefs:
        sid = server_id(ctx, o.key, h)
        d = data.get(h) if open.get(h) is not None else None
        if d is None:
            if open.get(h) is not None:
                ctx.log.event("eas_item_skipped", reason="not-representable", folder=o.key, request_id=ctx.request_id)
            if h in known:
                commands.append(node("AirSync:Delete", node("AirSync:ServerId", text=sid)))
                plan["delete"].append(h)
                del known[h]
            open.pop(h, None)
            continue
        kind = "AirSync:Change" if h in known else "AirSync:Add"
        element = node(kind, node("AirSync:ServerId", text=sid), d)
        length = len(wbxml.encode(element))
        if commands and size + length > budget:
            return commands, plan, True
        size += length
        commands.append(element)
        (plan["change"] if kind == "AirSync:Change" else plan["add"]).append(h)
        etag = visible[h].dav_object.etag
        plan["etags"][h] = etag
        known[h] = etag
        open.pop(h, None)
        enforce.pop(h, None)
    return commands, plan, False


# ------------------------------------------------------------------ one collection


@dataclass(slots=True)
class _Result:
    node: Node
    count: int = 0
    empty: bool = False
    state: EasSync | None = None
    command_hash: bytes = b""


def _ghosting(supported: Node | None) -> object:
    """No `Supported` in a Sync with SyncKey 0 => nothing ghosted ("none"); otherwise the list of non-ghosted calendar fields."""
    if supported is None:
        return "keins"
    return sorted({tags.display(k) for k in supported.children})[:40]


def _not_ghosted(options) -> frozenset[str]:
    value = options.get("ghost", "keins")
    if value == "keins":
        return frozenset(calendar.GHOSTBAR_DELETE)
    return frozenset(value) & frozenset(calendar.GHOSTBAR_DELETE)


async def _collection_edit(ctx: EasContext, c: Node, window_global: int | None) -> _Result:
    sk = tags.text_from(c, "AirSync:SyncKey")
    cid = tags.text_from(c, "AirSync:CollectionId")
    if not sk or not cid:
        return _Result(_collection(sk or "0", cid or "", 4))
    o = folder(ctx.account, cid)
    if o is None:
        return _Result(_collection(sk, cid, 12))
    commands_node = tags.find(c, "AirSync:Commands")
    command_list = commands_node.children if commands_node is not None else ()
    if len(command_list) > MAX_COMMANDS:
        return _Result(_collection(sk, cid, 4))
    command_hash = hashlib.sha256(wbxml.encode(commands_node) if commands_node is not None else b"").digest()
    get_changes = tags.text_from(c, "AirSync:GetChanges")
    new_options = read_options(c)
    if new_options and new_options.get("conflict") == 0:
        ctx.log.event("eas_conflict_option", folder=o.key, request_id=ctx.request_id)
    window_text = tags.text_from(c, "AirSync:WindowSize")
    window = int(window_text) if window_text and window_text.isdigit() else (window_global or 100)
    window = max(1, min(window, ctx.eas.window_max))

    async with ctx.locks.eas_sync(ctx.account.name, ctx.device, o.key):
        outcome = await ctx.devices.load(ctx, o.key, sk, command_hash)
        if outcome.mode == "invalid":
            ctx.log.event("eas_sync_invalid", reason=outcome.reason, folder=o.key, device=ctx.device, request_id=ctx.request_id)
            return _Result(_collection("0", cid, 3))
        if outcome.mode == "new":
            if get_changes == "1" or command_list:
                return _Result(_collection("0", cid, 4))
            open: dict[str, str | None] = {}
            token = None
            if o.cal is not None:
                delta = await get_delta(ctx, o.cal, None)
                open = {h: e for h, e in delta.changed.items()}
                token = delta.token
            options = dict(ctx.devices.options(None, new_options))
            options["ghost"] = _ghosting(tags.find(c, "AirSync:Supported"))
            z = EasSync(ctx.account.name, ctx.device, o.key, 1, token, {}, open, None, None, options, int(ctx.clock.now()))
            await ctx.devices.store(z, expected=None)
            return _Result(_collection(ctx.ids.eas_sync_key(ctx.account.name, ctx.device, o.key, 1), cid, 1))
        z = outcome.state
        assert z is not None
        options = ctx.devices.options(z.options, new_options)
        if outcome.mode == "repeat":
            ctx.log.event("eas_sync_retry", folder=o.key, device=ctx.device, request_id=ctx.request_id)
            cids = [tags.text_from(cmd, "AirSync:ClientId") or "" for cmd in command_list]
            return _Result(await _repetition(ctx, o, z, options, cids))
        return await _normal(ctx, o, z, command_list, get_changes != "0", window, options, command_hash)


async def _command(run: _Run, cmd: Node, right: Permissions, *, name: str) -> None:
    o = run.o
    if o.cal is None:
        if name == "AirSync:Add":
            run.responses.append({"kind": "add", "cid": (tags.text_from(cmd, "AirSync:ClientId") or "")[:64], "href": None, "status": 6})
        elif name in ("AirSync:Change", "AirSync:Delete", "AirSync:Fetch"):
            kind = {"AirSync:Change": "change", "AirSync:Delete": "delete", "AirSync:Fetch": "fetch"}[name]
            run.responses.append({"kind": kind, "sid": (tags.text_from(cmd, "AirSync:ServerId") or "")[:12], "href": None, "status": 8})
        return
    if name == "AirSync:Add":
        await _add(run, cmd, right)
    elif name == "AirSync:Change":
        await _change(run, cmd, right)
    elif name == "AirSync:Delete":
        await _delete(run, cmd, right)
    elif name == "AirSync:Fetch":
        await _fetch(run, cmd)


async def _normal(ctx: EasContext, o: EasFolder, z: EasSync, command_list, fetch: bool, window: int, options, command_hash: bytes) -> _Result:
    types, trunc = body_options(options)
    run = _Run(
        ctx,
        o,
        dict(z.known),
        dict(z.open),
        not_ghosted=_not_ghosted(options),
        truncation=min(trunc or ctx.eas.body_max_chars, ctx.eas.body_max_chars),
        conflicts={k: list(v) for k, v in dict(options.get("k", {})).items() if isinstance(v, (list, tuple)) and len(v) == 2},
    )
    if command_list:
        right = await permissions_for(ctx, o)
        for i, cmd in enumerate(command_list):
            n0 = len(run.responses)
            await _command(run, cmd, right, name=tags.display(cmd))
            for a in run.responses[n0:]:
                a["i"] = i
    token = z.caldav_token
    if fetch and o.cal is not None:
        delta = await get_delta(ctx, o.cal, z.caldav_token)
        run.open = mix(run.known, run.open, delta)
        token = delta.token
    for h, etag in run.enforce.items():
        run.open[h] = etag
    commands: list[Node] = []
    plan_part = {"add": [], "change": [], "delete": [], "etags": {}}
    if fetch and run.open:
        front = [h for h in run.enforce if h in run.open]
        series = front + sorted(h for h in run.open if h not in run.enforce)
        commands, plan_part, _ = await _deliver(ctx, o, series[:window], options, run.open, run.known, run.enforce, ctx.eas.response_max_bytes)
    more = bool(run.open) and fetch
    responses = await _responses(ctx, o, run.responses, options)
    options = {**dict(options), "k": run.conflicts} if (run.conflicts or "k" in options) else dict(options)
    unchanged = not command_list and not commands and not run.responses and not more and token == z.caldav_token and run.known == dict(z.known) and run.open == dict(z.open)
    if unchanged:
        if options != dict(z.options):
            await ctx.devices.store(EasSync(z.account, z.device, z.folder, z.counter, z.caldav_token, z.known, z.open, z.plan, z.request_hash, options, z.last), expected=z.counter)
        else:
            await ctx.store.eas_touch(z.account, z.device, z.folder)
        key = ctx.ids.eas_sync_key(ctx.account.name, ctx.device, o.key, z.counter)
        return _Result(_collection(key, o.key, 1), 0, True, z, command_hash)
    plan = {**plan_part, "new": z.counter + 1, "responses": _without_cid(run.responses), "more": more}
    new = EasSync(z.account, z.device, z.folder, z.counter + 1, token, run.known, run.open, plan, command_hash, options, int(ctx.clock.now()))
    if not await ctx.devices.store(new, expected=z.counter):
        ctx.log.event("eas_sync_invalid", reason="cas", folder=o.key, device=ctx.device, request_id=ctx.request_id)
        return _Result(_collection("0", o.key, 3))
    key = ctx.ids.eas_sync_key(ctx.account.name, ctx.device, o.key, z.counter + 1)
    return _Result(_collection(key, o.key, 1, responses, more, commands), len(commands) + run.written)


async def _increase(ctx: EasContext, e: _Result) -> Node:
    """An empty collection in a response with a body still gets a new key (empty plan)."""
    z = e.state
    assert z is not None
    plan = {"add": [], "change": [], "delete": [], "etags": {}, "new": z.counter + 1, "responses": [], "more": False}
    new = EasSync(z.account, z.device, z.folder, z.counter + 1, z.caldav_token, z.known, z.open, plan, e.command_hash, z.options, int(ctx.clock.now()))
    if not await ctx.devices.store(new, expected=z.counter):
        return _collection("0", z.folder, 3)
    return _collection(ctx.ids.eas_sync_key(ctx.account.name, ctx.device, z.folder, z.counter + 1), z.folder, 1)


async def _repetition(ctx: EasContext, o: EasFolder, z: EasSync, options, cids: list[str]) -> Node:
    """Retry: rebuild the response from the plan; write nothing, do not apply client commands again."""
    plan = z.plan or {}
    responses = await _responses(ctx, o, list(plan.get("responses", [])), options, cids)
    commands: list[Node] = []
    hrefs = list(plan.get("add", [])) + list(plan.get("change", []))
    visible = await load_visible(ctx, o.cal, hrefs) if o.cal is not None and hrefs else {}
    for kind, listing in (("AirSync:Add", plan.get("add", [])), ("AirSync:Change", plan.get("change", []))):
        for h in listing:
            s = visible.get(h)
            d = _application_data(ctx, s, options) if s else None
            if d is not None:
                commands.append(node(kind, node("AirSync:ServerId", text=server_id(ctx, o.key, h)), d))
    for h in plan.get("delete", []):
        commands.append(node("AirSync:Delete", node("AirSync:ServerId", text=server_id(ctx, o.key, h))))
    key = ctx.ids.eas_sync_key(ctx.account.name, ctx.device, o.key, z.counter)
    return _collection(key, o.key, 1, responses, bool(plan.get("more")), commands)


# ------------------------------------------------------------------ Sync


def _number(text: str | None) -> int | None:
    return int(text) if text and text.isdigit() and len(text) <= 6 else None


def _limit(status: int, limit: int) -> EasResponse:
    return response(node("AirSync:Sync", node("AirSync:Status", text=status), node("AirSync:Limit", text=limit)), status)


@command("Sync")
async def sync(ctx: EasContext) -> EasResponse:
    payload = ctx.payload
    partial = payload is not None and tags.find(payload, "AirSync:Partial") is not None and tags.find(payload, "AirSync:Collections") is None
    if payload is None or partial:
        raw = ctx.devices.last_sync(ctx.account.name, ctx.device)
        if raw is None:
            return _sync_status(13)
        template = wbxml.decode(raw)
        if partial:
            over = [k for k in payload.children if tags.display(k) in ("AirSync:Wait", "AirSync:HeartbeatInterval", "AirSync:WindowSize")]
            rest = [k for k in template.children if tags.display(k) == "AirSync:Collections"]
            template = node("AirSync:Sync", *rest, *over)
        payload = template
    if tags.display(payload) != "AirSync:Sync":
        return _sync_status(4)
    collection = tags.find(payload, "AirSync:Collections")
    collections = tags.find_all(collection, "AirSync:Collection") if collection is not None else ()
    if not collections:
        return _sync_status(4)
    if len(collections) > MAX_COLLECTIONS:
        return _limit(15, MAX_COLLECTIONS)
    wait = tags.text_from(payload, "AirSync:Wait")
    hb = tags.text_from(payload, "AirSync:HeartbeatInterval")
    if wait is not None and hb is not None:
        return _sync_status(4)
    wait_s: int | None = None
    if wait is not None:
        w = _number(wait)
        if w is None or not 1 <= w <= ctx.eas.wait_max_minutes:
            return _limit(14, 1 if (w or 0) < 1 else ctx.eas.wait_max_minutes)
        wait_s = w * 60
    if hb is not None:
        h = _number(hb)
        if h is None or not ctx.eas.heartbeat_min_seconds <= h <= ctx.eas.heartbeat_max_seconds:
            return _limit(14, ctx.eas.heartbeat_min_seconds if (h or 0) < ctx.eas.heartbeat_min_seconds else ctx.eas.heartbeat_max_seconds)
        wait_s = h
    has_commands = any(tags.find(c, "AirSync:Commands") is not None for c in collections)
    if ctx.payload is not None and not partial and not has_commands and ctx.raw:
        ctx.devices.remember_sync(ctx.account.name, ctx.device, ctx.raw)
    window_global = _number(tags.text_from(payload, "AirSync:WindowSize"))
    until = ctx.clock.now() + wait_s if wait_s else 0.0
    while True:
        results = [await _collection_edit(ctx, c, window_global) for c in collections]
        all_empty = all(e.empty for e in results)
        if wait_s and not has_commands and all_empty and ctx.clock.now() < until:
            calendar_keys = [o.key for c in collections if (o := folder(ctx.account, tags.text_from(c, "AirSync:CollectionId"))) is not None and o.cal]
            reason = await ctx.waker.wait(ctx.account.name, ctx.device, calendar_keys, until, tick_s=ctx.config.poll_idle_seconds)
            if reason in ("signal", "poll"):
                continue
        break
    if all_empty:
        return EasResponse(None, 200, ("empty",))
    node_list = [await _increase(ctx, e) if e.empty else e.node for e in results]
    codes = [tags.text_from(k, "AirSync:Status") or "" for k in node_list]
    count = sum(e.count for e in results)
    return response(node("AirSync:Sync", node("AirSync:Collections", *node_list)), *codes, count=count)


# ------------------------------------------------------------------ GetItemEstimate


@command("GetItemEstimate")
async def get_item_estimate(ctx: EasContext) -> EasResponse:
    k = ctx.payload
    if k is None or tags.display(k) != "GetItemEstimate:GetItemEstimate":
        return response(node("GetItemEstimate:GetItemEstimate", node("GetItemEstimate:Response", node("GetItemEstimate:Status", text=2))), 2)
    collection = tags.find(k, "GetItemEstimate:Collections")
    responses: list[Node] = []
    codes: list[int] = []
    for c in (tags.find_all(collection, "GetItemEstimate:Collection") if collection is not None else ())[:MAX_COLLECTIONS]:
        cid = tags.text_from(c, "GetItemEstimate:CollectionId") or tags.text_from(c, "AirSync:CollectionId")
        sk = tags.text_from(c, "AirSync:SyncKey")
        o = folder(ctx.account, cid)
        status, estimate = 1, 0
        if o is None:
            status = 2
        elif not sk or sk == "0":
            status = 3
        else:
            outcome = await ctx.devices.load(ctx, o.key, sk, b"")
            if outcome.mode not in ("normal", "repeat") and not (outcome.mode == "invalid" and outcome.reason == "body"):
                status = 4
            elif o.cal is not None and outcome.state is not None:
                z = outcome.state
                delta = await get_delta(ctx, o.cal, z.caldav_token)
                estimate = len(mix(z.known, z.open, delta))
        codes.append(status)
        responses.append(
            node(
                "GetItemEstimate:Response",
                node("GetItemEstimate:Status", text=status),
                node("GetItemEstimate:Collection", node("GetItemEstimate:CollectionId", text=cid or ""), node("GetItemEstimate:Estimate", text=estimate))
                if status == 1
                else node("GetItemEstimate:Collection", node("GetItemEstimate:CollectionId", text=cid or "")),
            )
        )
    if not responses:
        return response(node("GetItemEstimate:GetItemEstimate", node("GetItemEstimate:Response", node("GetItemEstimate:Status", text=2))), 2)
    return response(node("GetItemEstimate:GetItemEstimate", *responses), *codes)
