"""Rejected commands: send, meeting responses, move, folder changes; search and recipient resolution are empty.
Responses carry the status values of the specification; the body is only read for echo fields (RequestId, SrcMsgId, To)."""

from __future__ import annotations

import re
from typing import Final

from ...identifiers import InvalidId
from .. import tags
from . import OWN_FOLDER, EasContext, EasResponse, command, folder_list, hierarchy_fingerprint, node, response

SEND_BLOCKED: Final = 167


def _echo(text: str | None) -> str:
    return (text or "")[:256]


async def _send(ctx: EasContext, root: str) -> EasResponse:
    return response(node(f"ComposeMail:{root}", node("ComposeMail:Status", text=SEND_BLOCKED)), SEND_BLOCKED)


@command("SendMail")
async def send_mail(ctx: EasContext) -> EasResponse:
    return await _send(ctx, "SendMail")


@command("SmartReply")
async def smart_reply(ctx: EasContext) -> EasResponse:
    return await _send(ctx, "SmartReply")


@command("SmartForward")
async def smart_forward(ctx: EasContext) -> EasResponse:
    return await _send(ctx, "SmartForward")


@command("MeetingResponse")
async def meeting_response(ctx: EasContext) -> EasResponse:
    k = ctx.payload
    results = []
    for r in (tags.find_all(k, "MeetingResponse:Request") if k is not None else ())[:50]:
        results.append(node("MeetingResponse:Result", node("MeetingResponse:RequestId", text=_echo(tags.text_from(r, "MeetingResponse:RequestId"))), node("MeetingResponse:Status", text=2)))
    if not results:
        results.append(node("MeetingResponse:Result", node("MeetingResponse:Status", text=2)))
    return response(node("MeetingResponse:MeetingResponse", *results), 2)


@command("MoveItems")
async def move_items(ctx: EasContext) -> EasResponse:
    k = ctx.payload
    responses = []
    for m in (tags.find_all(k, "Move:Move") if k is not None else ())[:50]:
        responses.append(node("Move:Response", node("Move:SrcMsgId", text=_echo(tags.text_from(m, "Move:SrcMsgId"))), node("Move:Status", text=2)))
    return response(node("Move:MoveItems", *responses), 2)


_NAME_ALLOWED = re.compile(r"[^\x00-\x1f\x7f]{1,64}")


async def _hierarchy_check(ctx: EasContext, key: str | None) -> tuple[int | None, list]:
    """(error status or None, own folders). The client must send the current hierarchy key, otherwise status 9."""
    own = await ctx.store.eas_folder_list(ctx.account.name, ctx.device)
    if not key or key == "0":
        return 9, own
    try:
        read_back = ctx.ids.read_eas_hierarchy_key(ctx.account.name, ctx.device, key)
    except InvalidId:
        return 9, own
    if read_back != hierarchy_fingerprint(ctx.account, own)[:8]:
        return 9, own
    return None, own


@command("FolderCreate")
async def folder_create(ctx: EasContext) -> EasResponse:
    """Microsoft's app creates a user-defined mail folder (type 12) during setup. We accept it as an empty folder remembered
    per device; everything else (calendar, tasks, ...) stays rejected (status 3)."""
    k = ctx.payload
    root = "FolderHierarchy:FolderCreate"

    def error(status: int) -> EasResponse:
        return response(node(root, node("FolderHierarchy:Status", text=status)), status)

    if k is None or tags.find(k, "FolderHierarchy:SyncKey") is None:
        return error(10)
    error_status, own = await _hierarchy_check(ctx, tags.text_from(k, "FolderHierarchy:SyncKey"))
    if error_status:
        return error(error_status)
    name = tags.text_from(k, "FolderHierarchy:DisplayName") or ""
    type = tags.text_from(k, "FolderHierarchy:Type") or ""
    parents = tags.text_from(k, "FolderHierarchy:ParentId") or "0"
    if not _NAME_ALLOWED.fullmatch(name) or not type.isdigit():
        return error(10)
    if int(type) != 12:
        return error(3)
    if parents != "0":
        return error(5)
    if name.lower() in {o.name.lower() for o in folder_list(ctx.account)} | {n.lower() for _, n, _ in own}:
        return error(2)
    folder_id = "u-" + ctx.ids.device_hash(ctx.account.name, ctx.device + "\x00" + name)[:10]
    if not await ctx.store.eas_folder_new(ctx.account.name, ctx.device, folder_id, name, 12):
        return error(6)
    new = await ctx.store.eas_folder_list(ctx.account.name, ctx.device)
    key = ctx.ids.eas_hierarchy_key(ctx.account.name, ctx.device, hierarchy_fingerprint(ctx.account, new))
    ctx.log.event("folder_created", device=ctx.device, value=12, request_id=ctx.request_id)
    return response(node(root, node("FolderHierarchy:Status", text=1), node("FolderHierarchy:SyncKey", text=key), node("FolderHierarchy:ServerId", text=folder_id)), 1)


@command("FolderDelete")
async def folder_delete(ctx: EasContext) -> EasResponse:
    k = ctx.payload
    root = "FolderHierarchy:FolderDelete"
    sid = tags.text_from(k, "FolderHierarchy:ServerId") if k is not None else None
    if sid is None or not OWN_FOLDER.fullmatch(sid):
        return response(node(root, node("FolderHierarchy:Status", text=3)), 3)
    error_status, _ = await _hierarchy_check(ctx, tags.text_from(k, "FolderHierarchy:SyncKey"))
    if error_status:
        return response(node(root, node("FolderHierarchy:Status", text=error_status)), error_status)
    if not await ctx.store.eas_folder_gone(ctx.account.name, ctx.device, sid):
        return response(node(root, node("FolderHierarchy:Status", text=4)), 4)
    new = await ctx.store.eas_folder_list(ctx.account.name, ctx.device)
    key = ctx.ids.eas_hierarchy_key(ctx.account.name, ctx.device, hierarchy_fingerprint(ctx.account, new))
    return response(node(root, node("FolderHierarchy:Status", text=1), node("FolderHierarchy:SyncKey", text=key)), 1)


@command("FolderUpdate")
async def folder_update(ctx: EasContext) -> EasResponse:
    return response(node("FolderHierarchy:FolderUpdate", node("FolderHierarchy:Status", text=2)), 2)


@command("Search")
async def search(ctx: EasContext) -> EasResponse:
    store = node("Search:Store", node("Search:Status", text=1), node("Search:Total", text=0))
    return response(node("Search:Search", node("Search:Status", text=1), node("Search:Response", store)), 1)


@command("ResolveRecipients")
async def resolve_recipients(ctx: EasContext) -> EasResponse:
    k = ctx.payload
    responses = []
    for to in (tags.find_all(k, "ResolveRecipients:To") if k is not None else ())[:50]:
        responses.append(
            node("ResolveRecipients:Response", node("ResolveRecipients:To", text=_echo(to.text)), node("ResolveRecipients:Status", text=4), node("ResolveRecipients:RecipientCount", text=0))
        )
    return response(node("ResolveRecipients:ResolveRecipients", node("ResolveRecipients:Status", text=1), *responses), 1)
