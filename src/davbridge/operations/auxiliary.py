"""Auxiliary operations with defined answers. Never a faked success for functions that do not exist."""

from __future__ import annotations

from lxml import etree

from .. import soap
from ..errors import Code, EwsError
from ..identifiers import InvalidId
from . import Context, Messages, Reply, operation, result, safe

NS = soap.NS
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, remove_comments=True)


def _response(name: str, code: Code, *content: etree._Element) -> Reply:
    w = soap.m(name, ResponseClass="Success" if code is Code.NoError else "Error")
    w.append(soap.m("ResponseCode", code.value))
    for i in content:
        w.append(i)
    return Reply(w, (code.value,))


# ---------------------------------------------------------------- UserConfiguration


def _config_location(ctx: Context, name_node: etree._Element) -> tuple[str, str]:
    name = name_node.get("Name", "")
    if not name or len(name) > 128:
        raise EwsError(Code.ErrorInvalidRequest)
    folder = next((k for k in name_node if etree.QName(k).localname in ("DistinguishedFolderId", "FolderId")), None)
    if folder is None:
        raise EwsError(Code.ErrorFolderNotFound)
    if etree.QName(folder).localname == "DistinguishedFolderId":
        return folder.get("Id", "")[:64], name
    try:
        return ctx.ids.read_folder_id(ctx.account.name, folder.get("Id", "")), name
    except InvalidId:
        raise EwsError(Code.ErrorInvalidIdMalformed) from None


def _name_node(root: etree._Element) -> etree._Element:
    for path in ("m:UserConfigurationName", "m:UserConfiguration/t:UserConfigurationName"):
        n = root.find(path, NS)
        if n is not None:
            return n
    raise EwsError(Code.ErrorInvalidRequest)


def _serialize(config: etree._Element) -> bytes:
    return b"".join(etree.tostring(k, encoding="utf-8", method="xml") for k in config if etree.QName(k).localname != "UserConfigurationName")


@operation("GetUserConfiguration")
async def get_user_configuration(ctx: Context) -> Messages:
    async def run():
        folder, name = _config_location(ctx, _name_node(ctx.request.payload))
        data = await ctx.store.config_get(ctx.account.name, folder, name)
        if data is None:
            raise EwsError(Code.ErrorItemNotFound)
        wrapper = etree.fromstring(b'<x xmlns:t="%s" xmlns:m="%s">%s</x>' % (soap.TYPES_NS.encode(), soap.MESSAGES_NS.encode(), data), PARSER)
        config = soap.m("UserConfiguration")
        for k in wrapper:
            config.append(k)
        return soap.message("GetUserConfiguration", Code.NoError, config)

    return result("GetUserConfiguration", [await safe("GetUserConfiguration", run())])


async def _store(ctx: Context, op: str, new: bool) -> Messages:
    async def run():
        config = ctx.request.payload.find("m:UserConfiguration", NS)
        if config is None:
            raise EwsError(Code.ErrorInvalidRequest)
        folder, name = _config_location(ctx, _name_node(ctx.request.payload))
        data = _serialize(config)
        try:
            ok = await ctx.store.config_set(ctx.account.name, folder, name, data, new=new)
        except ValueError:
            raise EwsError(Code.ErrorInvalidRequest) from None
        if not ok:
            raise EwsError(Code.ErrorItemSave)
        return soap.message(op, Code.NoError)

    return result(op, [await safe(op, run())])


@operation("CreateUserConfiguration")
async def create_user_configuration(ctx: Context) -> Messages:
    return await _store(ctx, "CreateUserConfiguration", True)


@operation("UpdateUserConfiguration")
async def update_user_configuration(ctx: Context) -> Messages:
    return await _store(ctx, "UpdateUserConfiguration", False)


@operation("DeleteUserConfiguration")
async def delete_user_configuration(ctx: Context) -> Messages:
    async def run():
        folder, name = _config_location(ctx, _name_node(ctx.request.payload))
        if not await ctx.store.config_delete(ctx.account.name, folder, name):
            raise EwsError(Code.ErrorItemNotFound)
        return soap.message("DeleteUserConfiguration", Code.NoError)

    return result("DeleteUserConfiguration", [await safe("DeleteUserConfiguration", run())])


# ---------------------------------------------------------------- truthfully empty


@operation("GetDelegate")
async def get_delegate(ctx: Context) -> Reply:
    return _response("GetDelegateResponse", Code.NoError, soap.m("ResponseMessages"), soap.m("DeliverMeetingRequests", "DelegatesAndMe"))


@operation("GetAppManifests")
async def get_app_manifests(ctx: Context) -> Reply:
    return _response("GetAppManifestsResponse", Code.NoError, soap.m("Manifests"))


@operation("GetRoomLists")
async def get_room_lists(ctx: Context) -> Reply:
    return _response("GetRoomListsResponse", Code.NoError, soap.m("RoomLists"))


@operation("GetUserPhoto")
async def get_user_photo(ctx: Context) -> Reply:
    return _response("GetUserPhotoResponse", Code.ErrorItemNotFound)


async def _oof(ctx: Context) -> Reply:
    """Out-of-office is not supported: the own mailbox always reports a disabled state, another mailbox does not exist here."""
    w = soap.m("GetUserOofSettingsResponse")
    address = ctx.request.payload.findtext("t:Mailbox/t:Address", None, NS)
    if address and address.strip().lower() not in (ctx.account.address.lower(), ctx.account.name.lower()):
        msg = soap.m("ResponseMessage", ResponseClass="Error")
        msg.append(soap.m("ResponseCode", Code.ErrorNonExistentMailbox.value))
        w.append(msg)
        return Reply(w, (Code.ErrorNonExistentMailbox.value,))
    msg = soap.m("ResponseMessage", ResponseClass="Success")
    msg.append(soap.m("ResponseCode", "NoError"))
    w.append(msg)
    oof = soap.t("OofSettings")
    oof.append(soap.t("OofState", "Disabled"))
    oof.append(soap.t("ExternalAudience", "None"))
    for name in ("InternalReply", "ExternalReply"):
        r = soap.t(name)
        r.append(soap.t("Message"))
        oof.append(r)
    w.append(oof)
    w.append(soap.m("AllowExternalOof", "None"))
    return Reply(w, ("NoError",))


operation("GetUserOofSettingsRequest")(_oof)
operation("GetUserOofSettings")(_oof)


@operation("SendItem")
async def send_item(ctx: Context) -> Messages:
    return result("SendItem", [soap.message("SendItem", Code.ErrorAccessDenied)])
