import time

import pytest
from tests import soap_helpers as h
from tests.unit.conftest import USER, codes, messages_xml, ops, post

from davbridge.identifiers import ElementRef
from davbridge.subscriptions import Event, EventKind

NS = h.NS


async def subscription_streaming(d, folder="calendar") -> str:
    e = await ops(d, h.subscribe_streaming(folder))
    assert codes(e) == ["NoError"]
    return messages_xml(e)[0].find("m:SubscriptionId", NS).text


def stream(result):
    assert hasattr(result, "chunks"), codes(result)
    return result.chunks


def status(chunk: bytes) -> str | None:
    e = h.parse(chunk).find(".//m:ConnectionStatus", NS)
    return e.text if e is not None else None


def report_item(d, clock, kind=EventKind.MODIFIED, href="a.ics", folder="test-calendar"):
    d.subscriptions.report(USER, folder, [Event(kind, folder, href, clock.now()), Event(EventKind.MODIFIED, folder, None, clock.now())])


async def test_first_chunk_immediately_and_every_chunk_is_a_parsable_envelope(services, clock):
    subscription = await subscription_streaming(services)
    gen = stream(await ops(services, h.get_streaming(subscription, 1)))
    start = clock.now()
    first = await gen.__anext__()
    assert clock.now() == start and status(first) == "OK"
    root = h.parse(first)
    assert root.tag == "{%s}Envelope" % h.S and root.find(".//m:GetStreamingEventsResponse", NS) is not None
    assert root.find(".//m:GetStreamingEventsResponseMessage", NS).get("ResponseClass") == "Success"
    await gen.aclose()


async def test_keepalive_every_thirty_seconds(services, clock):
    gen = stream(await ops(services, h.get_streaming(await subscription_streaming(services), 5)))
    await gen.__anext__()
    times = []
    for _ in range(3):
        chunk = await gen.__anext__()
        assert status(chunk) == "OK"
        times.append(clock.now())
    assert [b - a for a, b in zip(times, times[1:])] == [30, 30]
    await gen.aclose()


async def test_event_is_delivered_immediately(services, clock):
    subscription = await subscription_streaming(services)
    gen = stream(await ops(services, h.get_streaming(subscription, 5)))
    await gen.__anext__()
    report_item(services, clock)
    before = clock.now()
    chunk = await gen.__anext__()
    assert clock.now() == before
    w = h.parse(chunk)
    n = w.find(".//m:Notification", NS)
    assert n.find("t:SubscriptionId", NS).text == subscription
    kinds = [e.tag.split("}")[1] for e in n if e.tag.endswith("Event")]
    assert kinds == ["ModifiedEvent", "ModifiedEvent"]
    item = n.find(".//t:ItemId", NS)
    assert services.ids.read_element_id(USER, item.get("Id")) == ElementRef("test-calendar", "a.ics")
    assert n.find(".//t:FolderId", NS) is not None and n.find(".//t:ParentFolderId", NS) is not None
    assert n.find(".//t:FolderId", NS).get("ChangeKey") and n.find(".//t:UnreadCount", NS).text == "0"
    assert status(await gen.__anext__()) == "Closed"
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


@pytest.mark.parametrize("detail,expected", [("folder", 1), ("item", 1), ("both", 2)])
async def test_event_detail_folder_item_both(services_factory, clock, detail, expected):
    d = await services_factory(event_detail=detail)
    gen = stream(await ops(d, h.get_streaming(await subscription_streaming(d), 5)))
    await gen.__anext__()
    report_item(d, clock)
    n = h.parse(await gen.__anext__()).find(".//m:Notification", NS)
    assert len([e for e in n if e.tag.endswith("Event")]) == expected
    await gen.aclose()


@pytest.mark.parametrize("minutes,config,expected_s", [(1, {}, 60), (30, {}, 540), (30, {"stream_max_seconds": 120}, 120), (5, {"stream_max_seconds": 120}, 120)])
async def test_duration_is_minimum_of_timeout_and_stream_max(services_factory, clock, minutes, config, expected_s):
    d = await services_factory(**config)
    gen = stream(await ops(d, h.get_streaming(await subscription_streaming(d), minutes)))
    start = clock.now()
    last = None
    async for chunk in gen:
        last = chunk
    assert status(last) == "Closed" and clock.now() - start == expected_s


async def test_client_abort_cleans_up(services, clock):
    subscription = await subscription_streaming(services)
    a = services.subscriptions.get(USER, subscription, "streaming")
    gen = stream(await ops(services, h.get_streaming(subscription, 5)))
    await gen.__anext__()
    assert a.connection is not None and services.subscriptions.streams_active(USER) == 1
    await gen.aclose()
    assert a.connection is None and services.subscriptions.streams_active(USER) == 0


async def test_shutdown_ends_with_closed(services, clock):
    gen = stream(await ops(services, h.get_streaming(await subscription_streaming(services), 5)))
    await gen.__anext__()
    services.end.set()
    assert status(await gen.__anext__()) == "Closed"
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


async def test_new_stream_displaces_the_old_one_with_closed(services, clock):
    subscription = await subscription_streaming(services)
    old = stream(await ops(services, h.get_streaming(subscription, 5)))
    await old.__anext__()
    new = stream(await ops(services, h.get_streaming(subscription, 5)))
    assert status(await old.__anext__()) == "Closed"
    with pytest.raises(StopAsyncIteration):
        await old.__anext__()
    assert status(await new.__anext__()) == "OK"
    a = services.subscriptions.get(USER, subscription, "streaming")
    assert a.connection is not None
    await new.aclose()


def streaming_with(*ids: str, minutes: int = 1) -> str:
    listing = "".join(f"<t:SubscriptionId>{i}</t:SubscriptionId>" for i in ids)
    return f"<m:GetStreamingEvents><m:SubscriptionIds>{listing}</m:SubscriptionIds><m:ConnectionTimeout>{minutes}</m:ConnectionTimeout></m:GetStreamingEvents>"


async def test_invalid_subscription_next_to_valid_one_is_reported_the_rest_streams(services, clock):
    good = await subscription_streaming(services)
    broken = "broken-abo"
    gen = stream(await ops(services, streaming_with(good, broken)))
    first = h.parse(await gen.__anext__())
    error = [e.text for e in first.findall(".//m:ErrorSubscriptionIds/t:SubscriptionId", NS)]
    assert error == [broken] and status(await gen.__anext__()) == "OK"
    await gen.aclose()


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr("davbridge.operations.notifications.UNKNOWN_SUBSCRIPTION_PAUSE_S", 0)


async def test_unknown_subscription_throttles_the_retry(services, monkeypatch):
    """After a restart of DAVbridge without a new Subscribe, Outlook asked about 15 times per second with the old ID (2026-11-02, 15,835 requests)."""
    monkeypatch.setattr("davbridge.operations.notifications.UNKNOWN_SUBSCRIPTION_PAUSE_S", 0.2)
    t = time.monotonic()
    assert codes(await ops(services, streaming_with("broken"))) == ["ErrorSubscriptionNotFound"]
    assert time.monotonic() - t >= 0.2


async def test_only_invalid_subscriptions_yield_normal_error_response(services):
    e = await ops(services, streaming_with("broken"))
    assert codes(e) == ["ErrorSubscriptionNotFound"]
    unknown = services.ids.subscription_id(USER, b"andere-epoche", b"\x01" * 8)
    assert codes(await ops(services, streaming_with(unknown))) == ["ErrorSubscriptionNotFound"]
    assert codes(await ops(services, "<m:GetStreamingEvents><m:SubscriptionIds/></m:GetStreamingEvents>")) == ["ErrorInvalidRequest"]


async def test_pull_subscription_in_stream_is_invalid(services):
    e = await ops(services, h.subscribe_pull())
    pull = messages_xml(e)[0].find("m:SubscriptionId", NS).text
    assert codes(await ops(services, streaming_with(pull))) == ["ErrorInvalidSubscription"]


async def test_too_many_streams_per_account(services):
    open = []
    for _ in range(4):
        gen = stream(await ops(services, h.get_streaming(await subscription_streaming(services), 5)))
        await gen.__anext__()
        open.append(gen)
    e = await ops(services, h.get_streaming(await subscription_streaming(services), 5))
    assert codes(e) == ["ErrorExceededConnectionCount"]
    for gen in open:
        await gen.aclose()


# ------------------------------------------------------------------ Subscribe / Unsubscribe / Pull


async def test_subscribe_all_folders_and_unknown_event_types(services):
    xml = (
        '<m:Subscribe><m:StreamingSubscriptionRequest SubscribeToAllFolders="true"><t:EventTypes><t:EventType>NewMailEvent</t:EventType>'
        "<t:EventType>MovedEvent</t:EventType></t:EventTypes></m:StreamingSubscriptionRequest></m:Subscribe>"
    )
    e = await ops(services, xml)
    assert codes(e) == ["NoError"]
    a = services.subscriptions.get(USER, messages_xml(e)[0].find("m:SubscriptionId", NS).text, "streaming")
    assert a.folder == {"test-calendar", "read-only-calendar"} and a.kinds == {EventKind.CREATED, EventKind.MODIFIED, EventKind.DELETED}


async def test_subscribe_without_folder_and_unknown_folder(services):
    assert codes(await ops(services, "<m:Subscribe><m:PullSubscriptionRequest><t:Timeout>5</t:Timeout></m:PullSubscriptionRequest></m:Subscribe>")) == ["ErrorInvalidRequest"]
    assert codes(await ops(services, h.subscribe_streaming("archivemsgfolderroot"))) == ["ErrorFolderNotFound"]
    assert codes(await ops(services, "<m:Subscribe/>")) == ["ErrorInvalidRequest"]


async def test_pull_expiry_get_events_watermark(services, clock):
    e = await ops(services, h.subscribe_pull())
    n = messages_xml(e)[0]
    subscription, w0 = n.find("m:SubscriptionId", NS).text, n.find("m:Watermark", NS).text
    report_item(services, clock, EventKind.CREATED)
    ev = await ops(services, h.get_events(subscription, w0))
    notif = messages_xml(ev)[0].find("m:Notification", NS)
    assert notif.find("t:PreviousWatermark", NS).text == w0 and notif.find("t:MoreEvents", NS).text == "false"
    assert [x.tag.split("}")[1] for x in notif if x.tag.endswith("Event")] == ["CreatedEvent", "ModifiedEvent"]
    w2 = [x.find("t:Watermark", NS).text for x in notif if x.tag.endswith("Event")][-1]
    again = await ops(services, h.get_events(subscription, w0))
    assert len([x for x in messages_xml(again)[0].find("m:Notification", NS) if x.tag.endswith("Event")]) == 2
    empty = await ops(services, h.get_events(subscription, w2))
    status = [x for x in messages_xml(empty)[0].find("m:Notification", NS) if x.tag.endswith("Event")]
    assert [x.tag.split("}")[1] for x in status] == ["StatusEvent"]
    assert status[0].find("t:Watermark", NS).text == w2
    assert codes(await ops(services, h.get_events(subscription, "broken"))) == ["ErrorInvalidWatermark"]
    clock.before(301)
    assert codes(await ops(services, h.get_events(subscription, w2))) == ["ErrorExpiredSubscription"]


async def test_unsubscribe_and_get_events_on_streaming_subscription(services):
    subscription = await subscription_streaming(services)
    w = services.ids.watermark(USER, b"x" * 8, 0)
    assert codes(await ops(services, h.get_events(subscription, w))) == ["ErrorInvalidSubscription"]
    assert codes(await ops(services, f"<m:Unsubscribe><m:SubscriptionId>{subscription}</m:SubscriptionId></m:Unsubscribe>")) == ["NoError"]
    assert codes(await ops(services, streaming_with(subscription))) == ["ErrorSubscriptionNotFound"]
    assert codes(await ops(services, "<m:Unsubscribe/>")) == ["ErrorInvalidRequest"]


# ------------------------------------------------------------------ HTTP level


async def test_http_stream_response_has_streaming_head_and_closed_at_the_end(bridge, clock):
    client, d = bridge
    w = await post(client, h.subscribe_streaming())
    subscription_id = w.find(".//m:SubscriptionId", NS).text
    report_item(d, clock)
    r = await client.post("/EWS/Exchange.asmx", content=h.envelope(h.get_streaming(subscription_id, 1)))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/xml")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-accel-buffering"] == "no" and "content-encoding" not in r.headers
    assert r.text.count("<?xml") == 1 and r.text.startswith("<?xml")
    chunks = [("<s:Envelope" + t).encode() for t in r.text.split("<s:Envelope")[1:]]
    assert len(chunks) == 3
    assert all(h.parse(c).tag == "{%s}Envelope" % h.S for c in chunks)
    assert status(chunks[0]) == "OK" and status(chunks[-1]) == "Closed"
    assert any(h.parse(c).find(".//m:Notification", NS) is not None for c in chunks)


async def test_event_item_id_carries_change_key_of_the_etag(services, clock):
    subscription = await subscription_streaming(services)
    gen = stream(await ops(services, h.get_streaming(subscription, 5)))
    await gen.__anext__()
    services.subscriptions.report(USER, "test-calendar", [Event(EventKind.CREATED, "test-calendar", "a.ics", clock.now(), '"etag-1"')])
    n = h.parse(await gen.__anext__()).find(".//m:Notification", NS)
    item = n.find(".//t:CreatedEvent/t:ItemId", NS)
    assert services.ids.read_change_key(USER, ElementRef("test-calendar", "a.ics"), item.get("ChangeKey")) == '"etag-1"'
    await gen.aclose()
