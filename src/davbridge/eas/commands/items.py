"""ItemOperations: Fetch only, on calendar items the device knows; Move/EmptyFolderContents => status 2."""

from __future__ import annotations

from .. import calendar, tags
from . import EasContext, EasResponse, body_options, command, folder, href_for, load_visible, node, response


@command("ItemOperations")
async def item_operations(ctx: EasContext) -> EasResponse:
    k = ctx.payload
    if k is None or tags.display(k) != "ItemOperations:ItemOperations":
        return response(node("ItemOperations:ItemOperations", node("ItemOperations:Status", text=2)), 2)
    if any(tags.display(x) in ("ItemOperations:Move", "ItemOperations:EmptyFolderContents") for x in k.children):
        return response(node("ItemOperations:ItemOperations", node("ItemOperations:Status", text=2)), 2)
    responses = []
    codes = []
    for f in tags.find_all(k, "ItemOperations:Fetch")[:50]:
        cid = tags.text_from(f, "AirSync:CollectionId")
        sid = tags.text_from(f, "AirSync:ServerId")
        o = folder(ctx.account, cid)
        status, data = 6, None
        if o is not None and o.cal is not None and sid:
            z = await ctx.store.eas_sync_read(ctx.account.name, ctx.device, o.key)
            href = href_for(ctx, o.key, sid, list(z.known) + list(z.open)) if z is not None else None
            if href is not None:
                s = (await load_visible(ctx, o.cal, [href])).get(href)
                if s is not None:
                    opt = {"body": []}
                    options = tags.find(f, "ItemOperations:Options")
                    if options is not None:
                        for bp in tags.find_all(options, "AirSyncBase:BodyPreference"):
                            type = tags.text_from(bp, "AirSyncBase:Type")
                            trunc = tags.text_from(bp, "AirSyncBase:TruncationSize")
                            if type and type.isdigit():
                                opt["body"].append({"type": int(type), "trunc": int(trunc) if trunc and trunc.isdigit() else None})
                    types, trunc = body_options(opt)
                    application = calendar.appointment_to_application_data(
                        s.appointment,
                        s.dav_object.ics or "",
                        body_types=types,
                        trunc=trunc,
                        body_max=ctx.eas.body_max_chars,
                        account_name=ctx.account.display_name,
                        account_address=ctx.account.address,
                    )
                    if application is not None:
                        status = 1
                        data = node("ItemOperations:Properties", *application.children)
        codes.append(status)
        responses.append(
            node(
                "ItemOperations:Fetch",
                node("ItemOperations:Status", text=status),
                node("AirSync:CollectionId", text=cid or ""),
                node("AirSync:ServerId", text=(sid or "")[:64]),
                node("AirSync:Class", text="Calendar") if status == 1 else None,
                data,
            )
        )
    return response(node("ItemOperations:ItemOperations", node("ItemOperations:Status", text=1), node("ItemOperations:Response", *responses)), 1, *codes)
