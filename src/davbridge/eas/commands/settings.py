"""Settings: read Oof (off), acknowledge DeviceInformation (only model/OS remembered), read UserInformation (SendDisabled)."""

from __future__ import annotations

import re
from typing import Final

from .. import tags
from . import EasContext, EasResponse, command, node, response

_PATTERN: Final = re.compile(r"[A-Za-z0-9 ._\-]{1,40}")


@command("Settings")
async def settings(ctx: EasContext) -> EasResponse:
    k = ctx.payload
    if k is None or tags.display(k) != "Settings:Settings":
        return response(node("Settings:Settings", node("Settings:Status", text=2)), 2)
    parts = []
    total = 1
    for kind in k.children:
        name = tags.display(kind)
        if name == "Settings:Oof":
            set_node = tags.find(kind, "Settings:Set")
            if set_node is not None and (tags.text_from(set_node, "Settings:OofState") or "") == "0":
                parts.append(node("Settings:Oof", node("Settings:Status", text=1)))  # "off" is the state anyway: success without effect
                continue
            if set_node is not None:
                total = 7
                continue
            parts.append(node("Settings:Oof", node("Settings:Status", text=1), node("Settings:Get", node("Settings:OofState", text=0))))
        elif name == "Settings:DeviceInformation":
            member_set = tags.find(kind, "Settings:Set")
            if member_set is not None:
                model = tags.text_from(member_set, "Settings:Model")
                os_ = tags.text_from(member_set, "Settings:OS")
                await ctx.store.eas_device_info(ctx.account.name, ctx.device, model if model and _PATTERN.fullmatch(model) else None, os_ if os_ and _PATTERN.fullmatch(os_) else None)
            parts.append(node("Settings:DeviceInformation", node("Settings:Status", text=1)))
        elif name == "Settings:UserInformation":

            def addresses():
                return node("Settings:EmailAddresses", node("Settings:SMTPAddress", text=ctx.account.address), node("Settings:PrimarySmtpAddress", text=ctx.account.address))

            account = node(
                "Settings:Account",
                node("Settings:AccountId", text="1"),
                node("Settings:AccountName", text=ctx.account.address),
                node("Settings:UserDisplayName", text=ctx.account.display_name),
                node("Settings:SendDisabled", text=1),
                addresses(),
            )
            parts.append(node("Settings:UserInformation", node("Settings:Status", text=1), node("Settings:Get", addresses(), node("Settings:Accounts", account))))
        elif name in ("Settings:DevicePassword", "Settings:RightsManagementInformation"):
            if tags.find(kind, "Settings:Set") is not None:
                total = 7
                continue
            parts.append(node(name, node("Settings:Status", text=1)))
    if total != 1:
        return response(node("Settings:Settings", node("Settings:Status", text=total)), total)
    return response(node("Settings:Settings", node("Settings:Status", text=1), *parts), 1)
