"""FolderSync: static hierarchy from the configuration; a delta is not worth it, an outdated key => status 9."""

from __future__ import annotations

from ...identifiers import InvalidId
from .. import tags
from . import EasContext, EasResponse, command, folder_list, hierarchy_fingerprint, node, response


def _error(status: int) -> EasResponse:
    return response(node("FolderHierarchy:FolderSync", node("FolderHierarchy:Status", text=status)), status)


@command("FolderSync")
async def folder_sync(ctx: EasContext) -> EasResponse:
    k = ctx.payload
    if k is None or tags.display(k) != "FolderHierarchy:FolderSync":
        return _error(10)
    sk = tags.text_from(k, "FolderHierarchy:SyncKey")
    if sk is None:
        return _error(10)
    own = await ctx.store.eas_folder_list(ctx.account.name, ctx.device)
    fingerprint = hierarchy_fingerprint(ctx.account, own)
    key = ctx.ids.eas_hierarchy_key(ctx.account.name, ctx.device, fingerprint)
    if sk != "0":
        try:
            old = ctx.ids.read_eas_hierarchy_key(ctx.account.name, ctx.device, sk)
        except InvalidId:
            return _error(9)
        if old != fingerprint[:8]:
            return _error(9)
        return response(
            node(
                "FolderHierarchy:FolderSync", node("FolderHierarchy:Status", text=1), node("FolderHierarchy:SyncKey", text=key), node("FolderHierarchy:Changes", node("FolderHierarchy:Count", text=0))
            ),
            1,
        )
    entries = [
        node(
            "FolderHierarchy:Add",
            node("FolderHierarchy:ServerId", text=o.key),
            node("FolderHierarchy:ParentId", text="0"),
            node("FolderHierarchy:DisplayName", text=o.name),
            node("FolderHierarchy:Type", text=o.type),
        )
        for o in folder_list(ctx.account)
    ]
    entries += [
        node(
            "FolderHierarchy:Add",
            node("FolderHierarchy:ServerId", text=k),
            node("FolderHierarchy:ParentId", text="0"),
            node("FolderHierarchy:DisplayName", text=n),
            node("FolderHierarchy:Type", text=t),
        )
        for k, n, t in own
    ]
    return response(
        node(
            "FolderHierarchy:FolderSync",
            node("FolderHierarchy:Status", text=1),
            node("FolderHierarchy:SyncKey", text=key),
            node("FolderHierarchy:Changes", node("FolderHierarchy:Count", text=len(entries)), *entries),
        ),
        1,
        count=len(entries),
    )
