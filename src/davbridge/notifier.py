"""CalDAV change reporter: one loop per (account, calendar) with its own sync token in RAM.
The notifier never modifies `sync_state` and never waits for consumers."""

from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .auth import Login
from .caldav import Caldav, CaldavError, CaldavKind
from .clock import Clock
from .config import Account, CalendarConfig, Config
from .event_log import EventLog
from .identifiers import ElementRef, IdService
from .store import Store
from .subscriptions import Event, EventKind, SubscriptionManagement

if TYPE_CHECKING:
    from .eas.wakeup import Waker

BACKOFF_START_S = 20
BACKOFF_MAX_S = 300
AUTH_PAUSE_S = 60
WITHOUT_ACCESS_S = 5


@dataclass(slots=True)
class _State:
    token: str | None = None
    known: set = field(default_factory=set)
    backoff: float = 0.0
    error_since: float | None = None
    last_ok: float | None = None


class Notifier:
    def __init__(
        self, config: Config, caldav: Caldav, subscriptions: SubscriptionManagement, login: Login, store: Store, clock: Clock, log: EventLog, ids: IdService, waker: "Waker | None" = None
    ) -> None:
        self._waker = waker
        self._config = config
        self._caldav = caldav
        self._subscriptions = subscriptions
        self._login = login
        self._store = store
        self._clock = clock
        self._protocol = log
        self._ids = ids
        self._state: dict[tuple[str, str], _State] = {}
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._end: asyncio.Event | None = None

    def watch(self, account: Account) -> None:
        """Idempotent; after every successful login. Starts the loops as soon as run() runs."""
        current = {cal.key for cal in account.calendar}
        for key in [k for k in self._tasks if k[0] == account.name and k[1] not in current]:
            self._tasks.pop(key).cancel()
            self._state.pop(key, None)
        for cal in account.calendar:
            key = (account.name, cal.key)
            self._state.setdefault(key, _State())
            if self._end is not None and key not in self._tasks:
                self._tasks[key] = asyncio.get_running_loop().create_task(self._loop(account, cal))

    def state(self, account: str, cal: str) -> _State | None:
        return self._state.get((account, cal))

    def caldav_error_since(self) -> float | None:
        times = [s.error_since for s in self._state.values() if s.error_since is not None]
        return min(times) if times else None

    def last_caldav_ok(self) -> float | None:
        times = [s.last_ok for s in self._state.values() if s.last_ok is not None]
        return max(times) if times else None

    async def run(self, end: asyncio.Event) -> None:
        self._end = end
        for account in self._config.accounts:
            if account.name in {k for k, _ in self._state}:
                self.watch(account)
        try:
            await end.wait()
        finally:
            for t in self._tasks.values():
                t.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def _tick(self, account: Account, cal: CalendarConfig) -> float:
        enabled = cal.key in self._subscriptions.watched(account.name) or (self._waker is not None and cal.key in self._waker.watched(account.name))
        base = self._config.poll_active_seconds if enabled else self._config.poll_idle_seconds
        return base * random.uniform(0.9, 1.1)

    async def _loop(self, account: Account, cal: CalendarConfig) -> None:
        while self._end is not None and not self._end.is_set():
            try:
                pause = await self.once(account, cal)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - exceptions never end the loop (only the type name is logged)
                self._protocol.error("notifier_exception", e, account=account.name, folder=cal.key)
                pause = BACKOFF_START_S
            await self._clock.sleep(pause)

    async def once(self, account: Account, cal: CalendarConfig) -> float:
        """One polling step. Returns the pause until the next one."""
        state = self._state.setdefault((account.name, cal.key), _State())
        credentials = self._login.access_for(account.name)
        if credentials is None:
            return WITHOUT_ACCESS_S
        now = self._clock.now()
        try:
            delta = await self._caldav.sync(credentials, cal, state.token)
        except CaldavError as e:
            if e.kind == CaldavKind.TOKEN_INVALID:
                state.token = None
                self._protocol.event("notifier_token_invalid", account=account.name, folder=cal.key)
                return 1.0
            if e.kind == CaldavKind.AUTH:
                self._login.forget(account.name, credentials)
                self._protocol.event("notifier_auth", account=account.name, folder=cal.key)
                return AUTH_PAUSE_S
            state.error_since = state.error_since or now
            state.backoff = min(BACKOFF_MAX_S, state.backoff * 2 if state.backoff else BACKOFF_START_S)
            self._protocol.event("notifier_error", account=account.name, folder=cal.key, caldav_kind=e.kind.value, expiry_s=int(state.backoff))
            return state.backoff
        state.backoff = 0.0
        state.error_since = None
        state.last_ok = now
        first_run = state.token is None
        events: list[Event] = []
        if first_run:
            state.known = set(delta.changed)
            events.append(Event(EventKind.MODIFIED, cal.key, None, now))
        else:
            for href, etag in delta.changed.items():
                kind = EventKind.MODIFIED if href in state.known else EventKind.CREATED
                state.known.add(href)
                events.append(Event(kind, cal.key, href, now, etag))
                await self._remember(account, cal, href, etag, now)
            for href in delta.deleted:
                state.known.discard(href)
                events.append(Event(EventKind.DELETED, cal.key, href, now))
                await self._remember(account, cal, href, "", now)
            if events:
                events.append(Event(EventKind.MODIFIED, cal.key, None, now))
        state.token = delta.token
        if events:
            self._subscriptions.report(account.name, cal.key, events)
            if self._waker is not None:
                self._waker.report(account.name, cal.key)
        return self._tick(account, cal)

    async def _remember(self, account: Account, cal: CalendarConfig, href: str, etag: str, now: float) -> None:
        etag_hash = hashlib.sha256(etag.encode()).hexdigest()[:8] if etag else None
        await self._store.event(account.name, "detected", folder=cal.key, element=self._ids.element_hash(account.name, ElementRef(cal.key, href)), etag_hash=etag_hash)
