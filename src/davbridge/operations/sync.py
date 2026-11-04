"""SyncFolderItems: a signed reference to an immutable record in SQLite."""

from __future__ import annotations

from .. import soap
from ..caldav import CaldavError, CaldavKind
from ..errors import Code, EwsError
from ..identifiers import ElementRef, InvalidId
from . import Context, Messages, deliver, folder_from_xml, folder_node, item_id, load_visible, operation, result, safe, shape

DEFAULT_MAX = 512
HARD_LIMIT = 1000


@operation("SyncFolderItems")
async def sync_folder_items(ctx: Context) -> Messages:
    k = ctx.request.payload
    complete = shape(k) != "IdOnly"
    raw_max = (k.findtext("m:MaxChangesReturned", str(DEFAULT_MAX), soap.NS) or str(DEFAULT_MAX)).strip()
    if not raw_max.isdigit() or int(raw_max) < 1:
        return result("SyncFolderItems", [soap.message("SyncFolderItems", Code.ErrorInvalidRequest)])
    max_changes = min(HARD_LIMIT, int(raw_max))

    async def run():
        node = folder_node(k, "m:SyncFolderId")
        if not node:
            raise EwsError(Code.ErrorFolderNotFound)
        folder = folder_from_xml(ctx, node[0])
        old_text = k.findtext("m:SyncState", None, soap.NS)
        if folder.cal is None:
            return _empty(ctx, folder.key, old_text)
        cal = folder.cal
        account = ctx.account.name

        state = None
        if old_text:
            try:
                zid = ctx.ids.read_sync_state(account, folder.key, old_text)
            except InvalidId:
                raise EwsError(Code.ErrorInvalidSyncStateData) from None
            state = await ctx.store.state(account, folder.key, zid)
            if state is None:
                raise EwsError(Code.ErrorInvalidSyncStateData)

        known: dict[str, str] = dict(state.known) if state else {}
        open: dict[str, str | None] = dict(state.open) if state else {}
        try:
            delta = await ctx.caldav.sync(ctx.credentials, cal, state.caldav_token if state else None)
        except CaldavError as e:
            if e.kind != CaldavKind.TOKEN_INVALID:
                raise
            delta = await ctx.caldav.sync(ctx.credentials, cal, None)

        if delta.full:
            current = dict(delta.changed)
            for h, etag in current.items():
                if open.get(h, known.get(h)) != etag:
                    open[h] = etag
            for h in (set(known) | {x for x, v in open.items() if v is not None}) - set(current):
                open[h] = None
        else:
            for h, etag in delta.changed.items():
                open[h] = etag
            for h in delta.deleted:
                open[h] = None
        for h in list(open):
            if open[h] is None and h not in known:
                del open[h]
            elif open[h] is not None and known.get(h) == open[h]:
                del open[h]

        candidates = sorted(open)[:max_changes]
        visible = await load_visible(ctx, cal, [h for h in candidates if open[h] is not None])

        changes = soap.m("Changes")
        delivered = 0
        for h in candidates:
            ref = ElementRef(folder.key, h)
            hit = visible.get(h) if open[h] is not None else None
            if hit is None:
                if h in known:
                    delete = soap.t("Delete")
                    delete.append(item_id(ctx, ref))
                    changes.append(delete)
                    delivered += 1
                    del known[h]
                open.pop(h, None)
                continue
            dav_object, appointment = hit
            kind = "Update" if h in known else "Create"
            wrapper = soap.t(kind)
            wrapper.append(await deliver(ctx, folder, ref, dav_object, appointment, complete))
            changes.append(wrapper)
            delivered += 1
            known[h] = dav_object.etag
            open.pop(h, None)

        equal_state = state is not None and delivered == 0 and delta.token == state.caldav_token and dict(state.known) == known and dict(state.open) == open
        if equal_state:
            new_text = old_text
        else:
            new_id = await ctx.store.new_state(account, folder.key, delta.token, known, open)
            new_text = ctx.ids.sync_state(account, folder.key, new_id)
        last = "false" if open else "true"
        return soap.message("SyncFolderItems", Code.NoError, soap.m("SyncState", new_text), soap.m("IncludesLastItemInRange", last), changes)

    return result("SyncFolderItems", [await safe("SyncFolderItems", run())])


def _empty(ctx: Context, key: str, old: str | None):
    """Mail folders are fixed and empty: success with an empty set and a fixed SyncState."""
    fixed = ctx.ids.sync_state(ctx.account.name, key, b"\x00" * 16)
    return soap.message("SyncFolderItems", Code.NoError, soap.m("SyncState", fixed), soap.m("IncludesLastItemInRange", "true"), soap.m("Changes"))
