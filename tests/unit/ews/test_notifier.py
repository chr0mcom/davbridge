import asyncio

import pytest
from tests import soap_helpers as h
from tests.doubles.fake_nextcloud import ics
from tests.unit.conftest import PASSWORD, TEST_CAL, USER, messages_xml, ops

from davbridge.caldav import CaldavError, CaldavKind
from davbridge.notifier import AUTH_PAUSE_S, BACKOFF_MAX_S, BACKOFF_START_S, WITHOUT_ACCESS_S, Notifier
from davbridge.subscriptions import EventKind

NS = h.NS


@pytest.fixture
def cal(services):
    return services.config.accounts[0].calendar_after_key("test-calendar")


@pytest.fixture
async def with_access(services):
    await services.login.check(h.basic(USER, PASSWORD)["Authorization"], "10.0.0.1")
    return services


@pytest.fixture
def stream_subscription(services):
    return services.subscriptions.subscribe(USER, "streaming", frozenset({"test-calendar"}), frozenset(EventKind) - {EventKind.STATUS}, None)


def account(d):
    return d.config.accounts[0]


def reporter_new(d):
    return Notifier(d.config, d.caldav, d.subscriptions, d.login, d.store, d.clock, d.log, d.ids)


def kinds(subscription):
    return [(e.kind, e.href) for e in subscription.queue]


async def test_without_access_nothing_is_queried(services, fake, cal):
    assert await services.notifier.once(account(services), cal) == WITHOUT_ACCESS_S
    assert fake.sync_calls == []


async def test_first_run_reports_exactly_one_folder_event(with_access, fake, cal, stream_subscription):
    d = with_access
    k = fake.cal(USER, TEST_CAL)
    for i in range(3):
        k.write(f"t{i}.ics", ics(f"u{i}"))
    await d.notifier.once(account(d), cal)
    assert kinds(stream_subscription) == [(EventKind.MODIFIED, None)]
    assert fake.sync_calls == [None]
    assert d.notifier.state(USER, "test-calendar").known == {"t0.ics", "t1.ics", "t2.ics"}


async def test_unchanged_no_events(with_access, fake, cal, stream_subscription):
    d = with_access
    await d.notifier.once(account(d), cal)
    stream_subscription.queue.clear()
    await d.notifier.once(account(d), cal)
    assert not stream_subscription.queue and fake.sync_calls[1] is not None


async def test_create_modified_deleted_with_folder_event(with_access, fake, cal, stream_subscription):
    d = with_access
    k = fake.cal(USER, TEST_CAL)
    k.write("a.ics", ics("ua"))
    k.write("b.ics", ics("ub"))
    await d.notifier.once(account(d), cal)
    stream_subscription.queue.clear()
    k.write("neu.ics", ics("un"))
    k.write("a.ics", ics("ua", "Changed"))
    k.remove("b.ics")
    await d.notifier.once(account(d), cal)
    assert sorted(kinds(stream_subscription), key=str) == sorted([(EventKind.CREATED, "neu.ics"), (EventKind.MODIFIED, "a.ics"), (EventKind.DELETED, "b.ics"), (EventKind.MODIFIED, None)], key=str)


async def test_tick_active_20s_idle_120s_with_jitter(with_access, fake, cal, stream_subscription):
    d = with_access
    for _ in range(10):
        pause = await d.notifier.once(account(d), cal)
        assert 18 <= pause <= 22
    d.subscriptions.cancel(USER, d.subscriptions.subscription_id(stream_subscription))
    for _ in range(10):
        pause = await d.notifier.once(account(d), cal)
        assert 108 <= pause <= 132


async def test_token_invalid_leads_to_full_sync_and_folder_event(with_access, fake, cal, stream_subscription):
    d = with_access
    k = fake.cal(USER, TEST_CAL)
    k.write("a.ics", ics("ua"))
    await d.notifier.once(account(d), cal)
    stream_subscription.queue.clear()
    k.min_token = k.n + 1
    k.n += 1
    assert await d.notifier.once(account(d), cal) == 1.0
    assert d.notifier.state(USER, "test-calendar").token is None
    await d.notifier.once(account(d), cal)
    assert kinds(stream_subscription) == [(EventKind.MODIFIED, None)]


async def test_auth_error_forgets_access_and_pauses(with_access, fake, cal):
    d = with_access
    fake.auth_error_on_sync = True
    assert await d.notifier.once(account(d), cal) == AUTH_PAUSE_S
    assert d.login.access_for(USER) is None
    assert await d.notifier.once(account(d), cal) == WITHOUT_ACCESS_S


@pytest.mark.parametrize("kind", [CaldavKind.BUSY, CaldavKind.NETWORK])
async def test_backoff_doubles_up_to_the_limit_and_recovers(with_access, fake, cal, clock, kind):
    d = with_access
    fake.failure = CaldavError(kind, 503)
    pauses = [await d.notifier.once(account(d), cal) for _ in range(7)]
    assert pauses == [BACKOFF_START_S, 40, 80, 160, BACKOFF_MAX_S, BACKOFF_MAX_S, BACKOFF_MAX_S]
    assert d.notifier.caldav_error_since() == clock.now()
    fake.failure = None
    pause = await d.notifier.once(account(d), cal)
    assert 108 <= pause <= 132
    assert d.notifier.caldav_error_since() is None and d.notifier.last_caldav_ok() == clock.now()
    fake.failure = CaldavError(kind, 503)
    assert await d.notifier.once(account(d), cal) == BACKOFF_START_S


async def test_restart_reports_exactly_one_folder_event_again(with_access, fake, cal, stream_subscription):
    d = with_access
    fake.cal(USER, TEST_CAL).write("a.ics", ics("ua"))
    await d.notifier.once(account(d), cal)
    stream_subscription.queue.clear()
    new = reporter_new(d)
    await new.once(account(d), cal)
    assert kinds(stream_subscription) == [(EventKind.MODIFIED, None)]


async def test_reporter_does_not_change_sync_states(with_access, fake, cal):
    d = with_access
    k = fake.cal(USER, TEST_CAL)
    k.write("a.ics", ics("ua"))
    folder = d.ids.folder_id(USER, "test-calendar")
    first = await ops(d, h.sync_items(folder))
    s1 = messages_xml(first)[0].find("m:SyncState", NS).text
    count = await d.store.count_states()
    k.write("neu.ics", ics("un"))
    for _ in range(3):
        await d.notifier.once(account(d), cal)
    assert await d.store.count_states() == count
    second = await ops(d, h.sync_items(folder, s1))
    assert [c.tag.split("}")[1] for c in messages_xml(second)[0].find("m:Changes", NS)] == ["Create"]


async def test_detection_is_logged_for_the_latency_measurement(with_access, fake, cal):
    d = with_access
    k = fake.cal(USER, TEST_CAL)
    await d.notifier.once(account(d), cal)
    k.write("a.ics", ics("ua"))
    await d.notifier.once(account(d), cal)
    lines = await d.store.events(kind="detected")
    assert len(lines) == 1 and len(lines[0][4]) == 12 and len(lines[0][5]) == 8


async def test_watch_is_idempotent_and_run_ends_with_end(services, fake, clock):
    """The loop runs with a clock that only advances the tick; `end` stops it and cleans up the tasks."""

    class YieldClock:
        def __init__(self, base):
            self.base = base

        def now(self):
            return self.base.now()

        async def sleep(self, s):
            self.base.before(s)
            await asyncio.sleep(0)

    d = services
    await d.login.check(h.basic(USER, PASSWORD)["Authorization"], "10.0.0.1")
    fake.cal(USER, TEST_CAL).write("a.ics", ics("ua"))
    m = Notifier(d.config, d.caldav, d.subscriptions, d.login, d.store, YieldClock(clock), d.log, d.ids)
    end = asyncio.Event()
    run = asyncio.create_task(m.run(end))
    await asyncio.sleep(0)
    m.watch(account(d))
    m.watch(account(d))
    assert len(m._tasks) == len(account(d).calendar)
    for _ in range(200):
        if len(fake.sync_calls) >= 6:
            break
        await asyncio.sleep(0)
    assert len(fake.sync_calls) >= 6
    end.set()
    await asyncio.wait_for(run, 2)
    assert all(t.done() for t in m._tasks.values())


async def test_watch_stops_the_loops_of_calendars_that_disappeared(services):
    import asyncio
    import dataclasses

    notifier = services.notifier
    started = []

    async def idle(account, cal):
        started.append(cal.key)
        await asyncio.sleep(3600)

    notifier._loop = idle
    notifier._end = asyncio.Event()
    account = services.config.accounts[0]
    type(notifier).watch(notifier, account)
    await asyncio.sleep(0)
    assert sorted(started) == sorted(c.key for c in account.calendar) and len(notifier._tasks) == 2
    kept = dataclasses.replace(account, calendar=account.calendar[:1])
    type(notifier).watch(notifier, kept)
    assert list(notifier._tasks) == [(account.name, account.calendar[0].key)]
    assert notifier.state(account.name, account.calendar[1].key) is None
    for task in notifier._tasks.values():
        task.cancel()
