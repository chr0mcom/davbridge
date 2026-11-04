"""Synthetic EWS requests for tests (invented values only)."""

from __future__ import annotations

import base64
from xml.sax.saxutils import escape

from lxml import etree

S = "http://schemas.xmlsoap.org/soap/envelope/"
M = "http://schemas.microsoft.com/exchange/services/2006/messages"
T = "http://schemas.microsoft.com/exchange/services/2006/types"
NS = {"s": S, "m": M, "t": T}


def envelope(payload: str, zone: str | None = None) -> bytes:
    header = '<t:RequestServerVersion Version="Exchange2016"/>'
    if zone:
        header += f'<t:TimeZoneContext><t:TimeZoneDefinition Id="{escape(zone)}"/></t:TimeZoneContext>'
    return (
        f'<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="{S}" xmlns:m="{M}" xmlns:t="{T}"><soap:Header>{header}</soap:Header><soap:Body>{payload}</soap:Body></soap:Envelope>'
    ).encode()


def basic(user: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode(), "Content-Type": "text/xml; charset=utf-8"}


def parse(content: bytes) -> etree._Element:
    return etree.fromstring(content)


def codes(root: etree._Element) -> list[str]:
    return [e.text for e in root.iter("{%s}ResponseCode" % M)]


def get_folder(*distinguished: str) -> str:
    ids = "".join(f'<t:DistinguishedFolderId Id="{d}"/>' for d in distinguished)
    return f"<m:GetFolder><m:FolderShape><t:BaseShape>AllProperties</t:BaseShape></m:FolderShape><m:FolderIds>{ids}</m:FolderIds></m:GetFolder>"


def sync_items(folder_id: str, state: str | None = None, shape: str = "IdOnly", max_n: int = 512) -> str:
    st = f"<m:SyncState>{state}</m:SyncState>" if state else ""
    return (
        f'<m:SyncFolderItems><m:ItemShape><t:BaseShape>{shape}</t:BaseShape></m:ItemShape><m:SyncFolderId><t:FolderId Id="{folder_id}"/></m:SyncFolderId>'
        f"{st}<m:MaxChangesReturned>{max_n}</m:MaxChangesReturned></m:SyncFolderItems>"
    )


def get_item(*item_ids: str) -> str:
    ids = "".join(f'<t:ItemId Id="{i}"/>' for i in item_ids)
    return f"<m:GetItem><m:ItemShape><t:BaseShape>AllProperties</t:BaseShape></m:ItemShape><m:ItemIds>{ids}</m:ItemIds></m:GetItem>"


def create_item(subject: str, start: str, end: str, location: str = "", folder: str = "calendar", extra: str = "") -> str:
    return (
        f'<m:CreateItem SendMeetingInvitations="SendToNone"><m:SavedItemFolderId><t:DistinguishedFolderId Id="{folder}"/></m:SavedItemFolderId><m:Items>'
        f"<t:CalendarItem><t:Subject>{escape(subject)}</t:Subject><t:Start>{start}</t:Start><t:End>{end}</t:End>"
        f"<t:Location>{escape(location)}</t:Location>{extra}</t:CalendarItem></m:Items></m:CreateItem>"
    )


def update_subject(item_id: str, change_key: str | None, subject: str) -> str:
    ck = f' ChangeKey="{change_key}"' if change_key else ""
    return (
        f'<m:UpdateItem ConflictResolution="AutoResolve" MessageDisposition="SaveOnly" SendMeetingInvitationsOrCancellations="SendToNone"><m:ItemChanges><t:ItemChange>'
        f'<t:ItemId Id="{item_id}"{ck}/><t:Updates><t:SetItemField><t:FieldURI FieldURI="item:Subject"/><t:CalendarItem><t:Subject>{escape(subject)}</t:Subject></t:CalendarItem></t:SetItemField></t:Updates>'
        f"</t:ItemChange></m:ItemChanges></m:UpdateItem>"
    )


def delete_item(item_id: str, change_key: str | None) -> str:
    ck = f' ChangeKey="{change_key}"' if change_key else ""
    return f'<m:DeleteItem DeleteType="MoveToDeletedItems" SendMeetingCancellations="SendToNone"><m:ItemIds><t:ItemId Id="{item_id}"{ck}/></m:ItemIds></m:DeleteItem>'


def subscribe_streaming(folder: str = "calendar") -> str:
    return (
        f'<m:Subscribe><m:StreamingSubscriptionRequest><t:FolderIds><t:DistinguishedFolderId Id="{folder}"/></t:FolderIds>'
        "<t:EventTypes><t:EventType>CreatedEvent</t:EventType><t:EventType>ModifiedEvent</t:EventType><t:EventType>DeletedEvent</t:EventType></t:EventTypes>"
        "</m:StreamingSubscriptionRequest></m:Subscribe>"
    )


def subscribe_pull(folder: str = "calendar") -> str:
    return (
        f'<m:Subscribe><m:PullSubscriptionRequest><t:FolderIds><t:DistinguishedFolderId Id="{folder}"/></t:FolderIds>'
        "<t:EventTypes><t:EventType>CreatedEvent</t:EventType><t:EventType>ModifiedEvent</t:EventType><t:EventType>DeletedEvent</t:EventType></t:EventTypes>"
        "<t:Timeout>5</t:Timeout></m:PullSubscriptionRequest></m:Subscribe>"
    )


def get_events(subscription: str, watermark: str) -> str:
    return f"<m:GetEvents><m:SubscriptionId>{subscription}</m:SubscriptionId><m:Watermark>{watermark}</m:Watermark></m:GetEvents>"


def get_streaming(subscription: str, minutes: int = 1) -> str:
    return (
        f"<m:GetStreamingEvents><m:SubscriptionIds><t:SubscriptionId>{subscription}</t:SubscriptionId></m:SubscriptionIds><m:ConnectionTimeout>{minutes}</m:ConnectionTimeout></m:GetStreamingEvents>"
    )
