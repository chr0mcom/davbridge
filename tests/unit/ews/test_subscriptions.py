import inspect

import pytest

from davbridge.errors import Code, EwsError
from davbridge.identifiers import IdService
from davbridge.subscriptions import DISPLACED, Event, EventKind, SubscriptionManagement

ALL = frozenset(EventKind) - {EventKind.STATUS}
K = "test-user"


@pytest.fixture
def ids():
    return IdService(bytes(range(32)))


@pytest.fixture
def subscriptions(ids, clock):
    return SubscriptionManagement(ids, clock)


def ev(clock, kind=EventKind.MODIFIED, folder="test-calendar", href="a.ics"):
    return Event(kind, folder, href, clock.now())


def new(subscriptions, type="streaming", folder=("test-calendar",), kinds=ALL, account=K, timeout=None):
    return subscriptions.subscribe(account, type, frozenset(folder), kinds, timeout)


def error_code(fn, *a):
    with pytest.raises(EwsError) as e:
        fn(*a)
    return e.value.code


def test_get_roundtrip_type_and_foreign_ids(subscriptions, ids):
    a = new(subscriptions)
    text = subscriptions.subscription_id(a)
    assert subscriptions.get(K, text, "streaming") is a
    assert error_code(subscriptions.get, K, text, "pull") == Code.ErrorInvalidSubscription
    assert error_code(subscriptions.get, K, "muell", "streaming") == Code.ErrorSubscriptionNotFound
    assert error_code(subscriptions.get, "other", text, "streaming") == Code.ErrorSubscriptionNotFound
    unknown = ids.subscription_id(K, subscriptions.epoch, b"\x00" * 8)
    assert error_code(subscriptions.get, K, unknown, "streaming") == Code.ErrorSubscriptionNotFound


def test_epoch_after_restart_is_not_found(subscriptions, ids, clock):
    text = subscriptions.subscription_id(new(subscriptions))
    restart = SubscriptionManagement(ids, clock)
    assert error_code(restart.get, K, text, "streaming") == Code.ErrorSubscriptionNotFound


def test_report_is_synchronous_and_returns_nothing(subscriptions, clock):
    assert not inspect.iscoroutinefunction(SubscriptionManagement.report)
    assert subscriptions.report(K, "test-calendar", [ev(clock)]) is None


def test_overflow_is_merged_into_one_folder_event(subscriptions, clock):
    a = new(subscriptions)
    subscriptions.report(K, "test-calendar", [ev(clock, href=f"{i}.ics") for i in range(SubscriptionManagement.MAX_QUEUE)])
    assert len(a.queue) == SubscriptionManagement.MAX_QUEUE and not a.overflow
    subscriptions.report(K, "test-calendar", [ev(clock, href="x.ics")])
    assert not a.queue and a.overflow == {"test-calendar"}
    subscriptions.report(K, "test-calendar", [ev(clock, href="y.ics")])
    assert not a.queue
    gen = subscriptions.connect([a])
    import asyncio

    hit = asyncio.run(subscriptions.wait([a], gen, clock.now() + 30))
    assert len(hit) == 1 and len(hit[0][1]) == 1
    e = hit[0][1][0]
    assert e.kind is EventKind.MODIFIED and e.href is None and e.folder == "test-calendar"
    assert not a.overflow and not a.queue


def test_filter_by_folder_kind_and_account(subscriptions, clock):
    only_deleted = new(subscriptions, kinds=frozenset({EventKind.DELETED}))
    other_folder = new(subscriptions, folder=("read-only-calendar",))
    foreign = new(subscriptions, account="other")
    subscriptions.report(K, "test-calendar", [ev(clock, EventKind.CREATED), ev(clock, EventKind.DELETED)])
    assert [e.kind for e in only_deleted.queue] == [EventKind.DELETED]
    assert not other_folder.queue and not foreign.queue


def test_watched_streams_and_cancel(subscriptions):
    a, b = new(subscriptions, folder=("test-calendar",)), new(subscriptions, folder=("read-only-calendar",))
    assert subscriptions.watched(K) == {"test-calendar", "read-only-calendar"} and subscriptions.watched("other") == frozenset()
    subscriptions.connect([a])
    assert subscriptions.streams_active(K) == 1
    subscriptions.cancel(K, subscriptions.subscription_id(b))
    assert subscriptions.watched(K) == {"test-calendar"}
    assert error_code(subscriptions.get, K, subscriptions.subscription_id(b), "streaming") == Code.ErrorSubscriptionNotFound
    subscriptions.cancel(K, "muell")


def test_pull_watermark_more_events_and_retry(subscriptions, clock):
    a = new(subscriptions, type="pull", timeout=5)
    subscriptions.report(K, "test-calendar", [ev(clock, href=f"{i}.ics") for i in range(60)])
    first, more = subscriptions.pull(a, None)
    assert [f for f, _ in first] == list(range(1, 51)) and more is True
    again, more2 = subscriptions.pull(a, None)
    assert [f for f, _ in again] == list(range(1, 51)) and more2 is True
    rest, more3 = subscriptions.pull(a, 50)
    assert [f for f, _ in rest] == list(range(51, 61)) and more3 is False
    empty, more4 = subscriptions.pull(a, 60)
    assert empty == [] and more4 is False


def test_pull_invalid_watermark(subscriptions, clock):
    a = new(subscriptions, type="pull", timeout=5)
    assert error_code(subscriptions.pull, a, 5) == Code.ErrorInvalidWatermark


def test_pull_subscription_expires_and_is_cleaned_up(subscriptions, clock):
    a = new(subscriptions, type="pull", timeout=5)
    text = subscriptions.subscription_id(a)
    clock.before(200)
    subscriptions.pull(a, None)
    clock.before(200)
    assert subscriptions.get(K, text, "pull") is a
    clock.before(301)
    assert error_code(subscriptions.get, K, text, "pull") == Code.ErrorExpiredSubscription
    b = new(subscriptions, type="pull", timeout=1)
    clock.before(61)
    subscriptions.clean_up()
    assert b not in subscriptions.all()


def test_stream_subscription_without_connection_is_removed_after_thirty_minutes(subscriptions, clock):
    a, b = new(subscriptions), new(subscriptions)
    subscriptions.connect([b])
    clock.before(1801)
    subscriptions.clean_up()
    assert a not in subscriptions.all() and b in subscriptions.all()
    subscriptions.split([b], b.connection)
    assert b.connection is None
    clock.before(1801)
    subscriptions.clean_up()
    assert b not in subscriptions.all()


def test_limit_subscriptions_per_account_discards_the_oldest(subscriptions, ids):
    first = new(subscriptions)
    text = subscriptions.subscription_id(first)
    for _ in range(SubscriptionManagement.MAX_SUBSCRIPTIONS_PER_ACCOUNT + 4):
        new(subscriptions)
    assert len(subscriptions.all(K)) == SubscriptionManagement.MAX_SUBSCRIPTIONS_PER_ACCOUNT
    assert error_code(subscriptions.get, K, text, "streaming") == Code.ErrorSubscriptionNotFound
    new(subscriptions, account="other")
    assert len(subscriptions.all("other")) == 1


def test_connected_subscriptions_are_never_displaced(subscriptions):
    first = new(subscriptions)
    generation = subscriptions.connect([first])
    for _ in range(SubscriptionManagement.MAX_SUBSCRIPTIONS_PER_ACCOUNT + 5):
        new(subscriptions)
    assert subscriptions.get(K, subscriptions.subscription_id(first), "streaming") is first
    assert first.connection == generation
    assert len(subscriptions.all(K)) == SubscriptionManagement.MAX_SUBSCRIPTIONS_PER_ACCOUNT


def test_only_connected_subscriptions_full_yields_error(subscriptions):
    subscriptions.connect([new(subscriptions) for _ in range(SubscriptionManagement.MAX_SUBSCRIPTIONS_PER_ACCOUNT)])
    assert error_code(new, subscriptions) == Code.ErrorExceededSubscriptionCount


async def test_end_wakes_waiting_streams(subscriptions, clock):
    a = new(subscriptions)
    generation = subscriptions.connect([a])
    import asyncio

    task = asyncio.ensure_future(subscriptions.wait([a], generation, until=clock.now() + 1000))
    await asyncio.sleep(0)
    subscriptions.end()
    assert await asyncio.wait_for(task, 2) == []


def test_pull_overflow_becomes_folder_event(subscriptions, clock):
    a = new(subscriptions, type="pull", folder=("a", "b"), timeout=30)
    for i in range(SubscriptionManagement.MAX_PULL_UNCONFIRMED + 20):
        a.unconfirmed.append((i + 1, ev(clock, folder="a", href=f"x{i}.ics")))
    a.sequence = len(a.unconfirmed)
    listing, _ = subscriptions.pull(a, None)
    assert any(e.href is None and e.folder == "a" for _, e in a.unconfirmed)


def test_limit_streams_per_account(subscriptions):
    for _ in range(SubscriptionManagement.MAX_STREAMS_PER_ACCOUNT):
        subscriptions.connect([new(subscriptions)])
    assert error_code(subscriptions.connect, [new(subscriptions)]) == Code.ErrorExceededConnectionCount
    subscriptions.connect([new(subscriptions, account="other")])


async def test_new_stream_displaces_the_old_one(subscriptions, clock):
    a = new(subscriptions)
    old = subscriptions.connect([a])
    new_gen = subscriptions.connect([a])
    assert new_gen != old and a.connection == new_gen
    assert await subscriptions.wait([a], old, clock.now() + 30) is DISPLACED
    subscriptions.split([a], old)
    assert a.connection == new_gen
    assert await subscriptions.wait([a], new_gen, clock.now() + 30) == []


async def test_wait_delivers_existing_events_immediately_and_empties(subscriptions, clock):
    a = new(subscriptions)
    gen = subscriptions.connect([a])
    subscriptions.report(K, "test-calendar", [ev(clock, EventKind.CREATED, href="x.ics")])
    start = clock.now()
    hit = await subscriptions.wait([a], gen, start + 30)
    assert clock.now() == start and [e.href for e in hit[0][1]] == ["x.ics"]
    assert await subscriptions.wait([a], gen, start + 30) == []


async def test_wait_without_event_runs_until_the_deadline(subscriptions, clock):
    a = new(subscriptions)
    gen = subscriptions.connect([a])
    start = clock.now()
    assert await subscriptions.wait([a], gen, start + 30) == []
    assert clock.now() >= start + 30
    assert await subscriptions.wait([a], gen, start) == []
