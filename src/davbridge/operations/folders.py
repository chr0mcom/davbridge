"""GetFolder, FindFolder, SyncFolderHierarchy (folder model)."""

from __future__ import annotations

from .. import soap
from ..errors import Code
from ..identifiers import InvalidId
from . import Context, Messages, children, folder_from_xml, folder_list, folder_node, folder_xml, hierarchy_fingerprint, operation, result, safe


@operation("GetFolder")
async def get_folder(ctx: Context) -> Messages:
    async def one(node):
        folder = folder_from_xml(ctx, node)
        folders = soap.m("Folders")
        folders.append(await folder_xml(ctx, folder))
        return soap.message("GetFolder", Code.NoError, folders)

    node = folder_node(ctx.request.payload, "m:FolderIds")
    if not node:
        return result("GetFolder", [soap.message("GetFolder", Code.ErrorFolderNotFound)])
    return result("GetFolder", [await safe("GetFolder", one(k)) for k in node])


@operation("FindFolder")
async def find_folder(ctx: Context) -> Messages:
    deep = ctx.request.payload.get("Traversal", "Shallow") == "Deep"

    async def one(node):
        parents = folder_from_xml(ctx, node)
        found = []
        open = [parents.key]
        while open:
            current = open.pop(0)
            for k in children(ctx, current):
                found.append(k)
                if deep:
                    open.append(k.key)
        listing = soap.t("Folders")
        for o in found:
            listing.append(await folder_xml(ctx, o))
        root = soap.m("RootFolder", TotalItemsInView=str(len(found)), IncludesLastItemInRange="true")
        root.append(listing)
        return soap.message("FindFolder", Code.NoError, root)

    node = folder_node(ctx.request.payload, "m:ParentFolderIds")
    if not node:
        return result("FindFolder", [soap.message("FindFolder", Code.ErrorFolderNotFound)])
    return result("FindFolder", [await safe("FindFolder", one(k)) for k in node])


@operation("SyncFolderHierarchy")
async def sync_folder_hierarchy(ctx: Context) -> Messages:
    fingerprint = hierarchy_fingerprint(ctx)
    new_state = ctx.ids.hierarchy_state(ctx.account.name, fingerprint)
    old = ctx.request.payload.findtext("m:SyncState", None, soap.NS)
    changes = soap.m("Changes")
    if old:
        try:
            if ctx.ids.read_hierarchy_state(ctx.account.name, old) == fingerprint:
                old = None
                equal = True
            else:
                equal = False
        except InvalidId:
            return result("SyncFolderHierarchy", [soap.message("SyncFolderHierarchy", Code.ErrorInvalidSyncStateData)])
    else:
        equal = False
    if not equal:
        kind = "Update" if ctx.request.payload.findtext("m:SyncState", None, soap.NS) else "Create"
        for o in folder_list(ctx.account):
            if o.key == "root":
                continue
            wrapper = soap.t(kind)
            wrapper.append(await folder_xml(ctx, o))
            changes.append(wrapper)
    content = [soap.m("SyncState", new_state), soap.m("IncludesLastFolderInRange", "true"), changes]
    return result("SyncFolderHierarchy", [soap.message("SyncFolderHierarchy", Code.NoError, *content)])
