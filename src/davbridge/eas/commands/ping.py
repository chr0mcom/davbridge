"""Ping: immediate check against CalDAV, then waiting for the waker with a periodic check; never a thread, never EWS subscriptions."""

from __future__ import annotations

from typing import Final

from .. import tags
from . import EasContext, EasResponse, command, folder, get_delta, load_visible, mix, node, response

MAX_FOLDER: Final = 50  # folder limit per ping, separate from ping_limit_per_user (simultaneously waiting pings)


def _status(status: int, *children) -> EasResponse:
    return response(node("Ping:Ping", node("Ping:Status", text=status), *children), status)


async def _changed(ctx: EasContext, folder_keys: list[str]) -> list[str]:
    from ...caldav import CaldavError

    changed: list[str] = []
    for key in folder_keys:
        o = folder(ctx.account, key)
        if o is None or o.cal is None:
            continue
        z = await ctx.store.eas_sync_read(ctx.account.name, ctx.device, key)
        if z is None:
            continue
        if z.open:
            changed.append(key)
            continue
        try:
            delta = await get_delta(ctx, o.cal, z.caldav_token)
        except CaldavError:
            raise
        if delta.full and z.caldav_token is not None:
            changed.append(key)
            continue
        change = mix(z.known, z.open, delta)
        if not change:
            continue
        new = [h for h, e in change.items() if e is not None]
        visible = await load_visible(ctx, o.cal, new) if new else {}
        if any(h in visible for h in new) or any(e is None for e in change.values()):
            changed.append(key)
    return changed


@command("Ping")
async def ping(ctx: EasContext) -> EasResponse:
    from ...caldav import CaldavError

    k = ctx.payload
    hb: int | None = None
    folder_list: list[tuple[str, str]] = []
    if k is not None:
        if tags.display(k) != "Ping:Ping":
            return _status(4)
        raw_hb = tags.text_from(k, "Ping:HeartbeatInterval")
        if raw_hb is not None:
            if not raw_hb.isdigit() or len(raw_hb) > 6:
                return _status(4)
            hb = int(raw_hb)
        folders = tags.find(k, "Ping:Folders")
        if folders is not None:
            for f in tags.find_all(folders, "Ping:Folder"):
                fid = tags.text_from(f, "Ping:Id")
                if not fid:
                    return _status(4)
                folder_list.append((fid, tags.text_from(f, "Ping:Class") or ""))
    old = ctx.devices.last_ping(ctx.account.name, ctx.device)
    if hb is None and old is not None:
        hb = old[0]
    if not folder_list and old is not None:
        folder_list = list(old[1])
    if hb is None or not folder_list:
        return _status(3)
    if hb < ctx.eas.heartbeat_min_seconds:
        return _status(5, node("Ping:HeartbeatInterval", text=ctx.eas.heartbeat_min_seconds))
    if hb > ctx.eas.heartbeat_max_seconds:
        return _status(5, node("Ping:HeartbeatInterval", text=ctx.eas.heartbeat_max_seconds))
    if len(folder_list) > MAX_FOLDER:
        return _status(6, node("Ping:MaxFolders", text=MAX_FOLDER))
    if any(folder(ctx.account, fid) is None for fid, _ in folder_list):
        return _status(7)
    ctx.devices.remember_ping(ctx.account.name, ctx.device, hb, tuple(folder_list))
    if ctx.waker.count(ctx.account.name) >= ctx.eas.ping_limit_per_user and ctx.device not in ctx.waker.devices(ctx.account.name):
        return EasResponse(None, 503, ("503",), {"Retry-After": "30"})
    keys = [fid for fid, _ in folder_list]
    until = ctx.clock.now() + hb
    start = ctx.clock.now()
    while True:
        try:
            changed = await _changed(ctx, keys)
        except CaldavError:
            ctx.log.event("eas_ping_end", reason="error", device=ctx.device, request_id=ctx.request_id)
            raise
        if changed:
            ctx.log.event("eas_ping_end", reason="change", duration_ms=int((ctx.clock.now() - start) * 1000), device=ctx.device, request_id=ctx.request_id)
            return _status(2, node("Ping:Folders", *(node("Ping:Folder", text=f) for f in changed)))
        calendar_keys = [key for key in keys if (o := folder(ctx.account, key)) is not None and o.cal is not None]
        reason = await ctx.waker.wait(ctx.account.name, ctx.device, calendar_keys, until, tick_s=ctx.config.poll_idle_seconds)
        if reason in ("signal", "poll") and ctx.clock.now() < until:
            continue
        ctx.log.event("eas_ping_end", reason=reason, duration_ms=int((ctx.clock.now() - start) * 1000), device=ctx.device, request_id=ctx.request_id)
        return _status(1)
