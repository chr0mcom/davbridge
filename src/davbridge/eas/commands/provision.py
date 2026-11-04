"""Provision: two phases, an empty permissive policy, never enforced. Policy keys are neither stored nor compared."""

from __future__ import annotations

from typing import Final

from .. import tags
from . import EasContext, EasResponse, command, node, response

POLICY_TYPE: Final = "MS-EAS-Provisioning-WBXML"
_ALLOWING: Final = (  # only schema elements, in schema order (MS-ASPROV 6.1)
    ("DevicePasswordEnabled", "0"),
    ("AlphanumericDevicePasswordRequired", "0"),
    ("PasswordRecoveryEnabled", "0"),
    ("AttachmentsEnabled", "1"),
    ("AllowSimpleDevicePassword", "1"),
    ("AllowCamera", "1"),
    ("RequireDeviceEncryption", "0"),
    ("AllowHTMLEmail", "1"),
    ("AllowBrowser", "1"),
)


def _key(ctx: EasContext, phase: int) -> str:
    return str(ctx.ids.policy_key(ctx.account.name, ctx.device, phase))


@command("Provision")
async def provision(ctx: EasContext) -> EasResponse:
    k = ctx.payload
    if k is None or tags.display(k) != "Provision:Provision":
        return response(node("Provision:Provision", node("Provision:Status", text=2)), 2)
    device_info = tags.find(k, "Settings:DeviceInformation")
    confirmation_info = None
    if device_info is not None:
        confirmation_info = node("Settings:DeviceInformation", node("Settings:Status", text=1))
        member_set = tags.find(device_info, "Settings:Set")
        if member_set is not None:
            await _remember_device(ctx, member_set)
    if tags.find(k, "Provision:RemoteWipe") is not None:
        return response(node("Provision:Provision", confirmation_info, node("Provision:Status", text=1)), 1)
    policy = tags.path(k, "Provision:Policies", "Provision:Policy")
    type = tags.text_from(policy, "Provision:PolicyType") if policy is not None else None
    if not type:
        return response(node("Provision:Provision", confirmation_info, node("Provision:Status", text=2)), 2)
    phase = 2 if tags.find(policy, "Provision:PolicyKey") is not None else 1
    ctx.log.event("eas_provision", phase=phase, device=ctx.device, request_id=ctx.request_id)
    if type != POLICY_TYPE:
        content = node("Provision:Policy", node("Provision:PolicyType", text=type[:64]), node("Provision:Status", text=3))
        return response(node("Provision:Provision", confirmation_info, node("Provision:Status", text=1), node("Provision:Policies", content)), 1, 3)
    if phase == 2:
        if tags.text_from(policy, "Provision:PolicyKey") != _key(ctx, 1):
            content = node("Provision:Policy", node("Provision:PolicyType", text=POLICY_TYPE), node("Provision:Status", text=5))
            return response(node("Provision:Provision", confirmation_info, node("Provision:Status", text=1), node("Provision:Policies", content)), 1, 5)
        content = node("Provision:Policy", node("Provision:PolicyType", text=POLICY_TYPE), node("Provision:Status", text=1), node("Provision:PolicyKey", text=_key(ctx, 2)))
    else:
        doc = node("Provision:EASProvisionDoc", *(node(f"Provision:{n}", text=w) for n, w in _ALLOWING))
        content = node(
            "Provision:Policy", node("Provision:PolicyType", text=POLICY_TYPE), node("Provision:Status", text=1), node("Provision:PolicyKey", text=_key(ctx, 1)), node("Provision:Data", doc)
        )
    return response(node("Provision:Provision", confirmation_info, node("Provision:Status", text=1), node("Provision:Policies", content)), 1)


async def _remember_device(ctx: EasContext, member_set) -> None:
    import re

    pattern = re.compile(r"[A-Za-z0-9 ._\-]{1,40}")
    model = tags.text_from(member_set, "Settings:Model")
    os_ = tags.text_from(member_set, "Settings:OS")
    model = model if model and pattern.fullmatch(model) else None
    os_ = os_ if os_ and pattern.fullmatch(os_) else None
    if model or os_:
        await ctx.store.eas_device_info(ctx.account.name, ctx.device, model, os_)
