"""Subscribe, Unsubscribe, GetEvents, GetStreamingEvents. Notifications are hints; Outlook fetches the data with SyncFolderItems."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from lxml import etree

from .. import soap
from ..errors import Code, EwsError
from ..identifiers import ElementRef, InvalidId
from ..subscriptions import DISPLACED, Event, EventKind, Subscription
from . import Context, Messages, Stream, folder_after_key, folder_from_xml, folder_id, folder_node, operation, parent_id, result, safe

NS = soap.NS
KEEPALIVE_S = 30
UNKNOWN_SUBSCRIPTION_PAUSE_S = 10
KINDS = {"CreatedEvent": EventKind.CREATED, "ModifiedEvent": EventKind.MODIFIED, "DeletedEvent": EventKind.DELETED}


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kinds(root: etree._Element) -> frozenset[EventKind]:
    desired = {KINDS[t.text.strip()] for t in root.findall("t:EventTypes/t:EventType", NS) if t.text and t.text.strip() in KINDS}
    return frozenset(desired or KINDS.values())


def _folder_key(ctx: Context, request: etree._Element) -> frozenset[str]:
    if request.get("SubscribeToAllFolders", "").lower() == "true":
        return frozenset(k.key for k in ctx.account.calendar)
    node = folder_node(request, "t:FolderIds")
    if not node:
        raise EwsError(Code.ErrorInvalidRequest)
    return frozenset(folder_from_xml(ctx, k).key for k in node)


@operation("Subscribe")
async def subscribe(ctx: Context) -> Messages:
    async def run():
        k = ctx.request.payload
        pull = k.find("m:PullSubscriptionRequest", NS)
        stream = k.find("m:StreamingSubscriptionRequest", NS)
        if pull is None and stream is None:
            raise EwsError(Code.ErrorInvalidRequest)
        request = pull if pull is not None else stream
        type = "pull" if pull is not None else "streaming"
        timeout = None
        if pull is not None:
            text = pull.findtext("t:Timeout", "30", NS) or "30"
            timeout = max(1, min(1440, int(text))) if text.isdigit() else 30
        subscription = ctx.subscriptions.subscribe(ctx.account.name, type, _folder_key(ctx, request), _kinds(request), timeout)
        await ctx.store.event(ctx.account.name, "subscription", value=1 if type == "streaming" else 0)
        content = [soap.m("SubscriptionId", ctx.subscriptions.subscription_id(subscription))]
        if type == "pull":
            content.append(soap.m("Watermark", ctx.ids.watermark(ctx.account.name, subscription.id, 0)))
        return soap.message("Subscribe", Code.NoError, *content)

    return result("Subscribe", [await safe("Subscribe", run())])


@operation("Unsubscribe")
async def unsubscribe(ctx: Context) -> Messages:
    async def run():
        value = ctx.request.payload.findtext("m:SubscriptionId", None, NS)
        if not value:
            raise EwsError(Code.ErrorInvalidRequest)
        ctx.subscriptions.cancel(ctx.account.name, value.strip())
        return soap.message("Unsubscribe", Code.NoError)

    return result("Unsubscribe", [await safe("Unsubscribe", run())])


def _event_xml(ctx: Context, e: Event, watermark: str | None) -> etree._Element | None:
    detail = ctx.config.event_detail
    if (e.href is None and detail == "item") or (e.href is not None and detail == "folder"):
        return None
    el = soap.t(e.kind.value)
    if watermark:
        el.append(soap.t("Watermark", watermark))
    el.append(soap.t("TimeStamp", _iso(e.moment)))
    folder = folder_after_key(ctx.account, e.folder)
    if e.href is not None:
        ref = ElementRef(e.folder, e.href)
        attr = {"Id": ctx.ids.element_id(ctx.account.name, ref)}
        if e.etag and e.kind is not EventKind.DELETED:
            attr["ChangeKey"] = ctx.ids.change_key(ctx.account.name, ref, e.etag)
        el.append(soap.t("ItemId", **attr))
        el.append(parent_id(ctx, e.folder))
    else:
        el.append(folder_id(ctx, e.folder, with_change_key=True))
        el.append(parent_id(ctx, folder.parents if folder else "msgfolderroot"))
        if e.kind is EventKind.MODIFIED:
            el.append(soap.t("UnreadCount", "0"))
    return el


def _notification(
    ctx: Context, subscription: Subscription, events: list[tuple[str | None, Event]], before: str | None = None, more: bool | None = None, status_marker: str | None = None
) -> etree._Element | None:
    """A notification needs at least one event (t:NotificationType). Without an event: StatusEvent with a watermark (pull) or `None` (stream)."""
    n = soap.m("Notification")
    n.append(soap.t("SubscriptionId", ctx.subscriptions.subscription_id(subscription)))
    if before is not None:
        n.append(soap.t("PreviousWatermark", before))
    if more is not None:
        n.append(soap.t("MoreEvents", "true" if more else "false"))
    number = 0
    for watermark, e in events:
        el = _event_xml(ctx, e, watermark)
        if el is not None:
            n.append(el)
            number += 1
    if number == 0:
        if status_marker is None:
            return None
        status = soap.t("StatusEvent")
        status.append(soap.t("Watermark", status_marker))
        n.append(status)
    return n


@operation("GetEvents")
async def get_events(ctx: Context) -> Messages:
    async def run():
        k = ctx.request.payload
        subscription_text = (k.findtext("m:SubscriptionId", "", NS) or "").strip()
        marker = (k.findtext("m:Watermark", "", NS) or "").strip()
        subscription = ctx.subscriptions.get(ctx.account.name, subscription_text, "pull")
        try:
            sequence = ctx.ids.read_watermark(ctx.account.name, subscription.id, marker) if marker else None
        except InvalidId:
            raise EwsError(Code.ErrorInvalidWatermark) from None
        listing, more = ctx.subscriptions.pull(subscription, sequence)
        pairs = [(ctx.ids.watermark(ctx.account.name, subscription.id, f), e) for f, e in listing]
        before = marker or ctx.ids.watermark(ctx.account.name, subscription.id, 0)
        marker = pairs[-1][0] if pairs else before
        return soap.message("GetEvents", Code.NoError, _notification(ctx, subscription, pairs, before, more, status_marker=marker))

    return result("GetEvents", [await safe("GetEvents", run())])


@operation("GetStreamingEvents")
async def get_streaming_events(ctx: Context) -> Messages | Stream:
    k = ctx.request.payload
    ids = [(t.text or "").strip() for t in k.findall("m:SubscriptionIds/t:SubscriptionId", NS)]
    if not ids:
        return result("GetStreamingEvents", [soap.message("GetStreamingEvents", Code.ErrorInvalidRequest)])
    subscriptions: list[Subscription] = []
    invalid: list[str] = []
    last: EwsError | None = None
    for i in ids:
        try:
            subscriptions.append(ctx.subscriptions.get(ctx.account.name, i, "streaming"))
        except EwsError as e:
            invalid.append(i)
            last = e
    if not subscriptions:
        e = last or EwsError(Code.ErrorSubscriptionNotFound)
        await asyncio.sleep(UNKNOWN_SUBSCRIPTION_PAUSE_S)
        return result("GetStreamingEvents", [soap.message("GetStreamingEvents", e.code)])
    minutes = k.findtext("m:ConnectionTimeout", "30", NS) or "30"
    duration = min((max(1, min(30, int(minutes))) if minutes.isdigit() else 30) * 60, ctx.config.stream_max_seconds)
    try:
        generation = ctx.subscriptions.connect(subscriptions)
    except EwsError as e:
        return result("GetStreamingEvents", [soap.message("GetStreamingEvents", e.code, backoff_ms=e.backoff_ms)])
    return Stream(_stream(ctx, subscriptions, generation, duration, invalid))


def _status(status: str, faulty_subscriptions: list[str] | None = None) -> etree._Element:
    content = []
    if faulty_subscriptions:
        listing = soap.m("ErrorSubscriptionIds")
        for i in faulty_subscriptions:
            listing.append(soap.t("SubscriptionId", i))
        content.append(listing)
    content.append(soap.m("ConnectionStatus", status))
    return soap.message("GetStreamingEvents", Code.NoError, *content)


async def _stream(ctx: Context, subscriptions: list[Subscription], generation: int, duration_s: int, invalid: list[str] | None = None) -> AsyncIterator[bytes]:
    """After an event chunk the stream ends with `Closed`. Outlook for Mac did not react to events inside an open
    stream; a completed response is evaluated by every client, and Outlook reconnects immediately (the events are then acknowledged)."""
    start = ctx.clock.now()
    end = start + duration_s
    reason = "aborted"
    try:
        yield soap.stream_chunk([_status("OK", invalid)], first=True)
        while True:
            now = ctx.clock.now()
            if now >= end or ctx.end.is_set():
                reason = "time" if now >= end else "shutdown"
                yield soap.stream_chunk([_status("Closed")])
                return
            hit = await ctx.subscriptions.wait(subscriptions, generation, until=min(end, now + KEEPALIVE_S))
            if hit is DISPLACED:
                reason = "superseded"
                yield soap.stream_chunk([_status("Closed")])
                return
            if not hit:
                if not ctx.end.is_set():
                    yield soap.stream_chunk([_status("OK")])
                continue
            reports = soap.m("Notifications")
            for subscription, events in hit:
                n = _notification(ctx, subscription, [(None, e) for e in events])
                if n is not None:
                    reports.append(n)
            if len(reports):
                ctx.log.event("stream_event", count=sum(len(m) for m in reports), duration_ms=int((ctx.clock.now() - start) * 1000), request_id=ctx.request_id)
                yield soap.stream_chunk([soap.message("GetStreamingEvents", Code.NoError, reports)])
                reason = "after-event"
                yield soap.stream_chunk([_status("Closed")])
                return
            else:
                yield soap.stream_chunk([_status("OK")])
    finally:
        ctx.log.event("stream_end", reason=reason, duration_ms=int((ctx.clock.now() - start) * 1000), request_id=ctx.request_id)
        ctx.subscriptions.split(subscriptions, generation)
