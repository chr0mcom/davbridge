"""Subscription management (RAM). Notifications are hints, not data: an overflow becomes a folder ModifiedEvent."""

from __future__ import annotations

import asyncio
import os
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Literal, Sequence

from .clock import Clock
from .errors import Code, EwsError
from .identifiers import IdService, InvalidId

STREAM_IDLE_S = 1800


class EventKind(StrEnum):
    CREATED = "CreatedEvent"
    MODIFIED = "ModifiedEvent"
    DELETED = "DeletedEvent"
    STATUS = "StatusEvent"


@dataclass(frozen=True, slots=True)
class Event:
    kind: EventKind
    folder: str
    href: str | None
    moment: float
    etag: str | None = None


@dataclass(slots=True)
class Subscription:
    id: bytes
    account: str
    type: Literal["streaming", "pull"]
    folder: frozenset[str]
    kinds: frozenset[EventKind]
    queue: deque = field(default_factory=deque)
    overflow: set = field(default_factory=set)
    signal: asyncio.Event = field(default_factory=asyncio.Event)
    sequence: int = 0
    acknowledged: int = 0
    unconfirmed: list = field(default_factory=list)
    expiry: float = 0.0
    pull_timeout_s: int | None = None
    connection: int | None = None
    last: float = 0.0


class Displaced:
    pass


DISPLACED: Final = Displaced()


class SubscriptionManagement:
    MAX_QUEUE: Final = 64
    MAX_SUBSCRIPTIONS_PER_ACCOUNT: Final = 20
    MAX_STREAMS_PER_ACCOUNT: Final = 4
    MAX_PULL_UNCONFIRMED: Final = 100
    PULL_BLOCK: Final = 50

    def __init__(self, ids: IdService, clock: Clock, epoch: bytes | None = None) -> None:
        self._ids = ids
        self._clock = clock
        self.epoch = epoch or os.urandom(8)
        self._subscriptions: dict[tuple[str, bytes], Subscription] = {}
        self._generation = 0
        self._end = False

    def end(self) -> None:
        """Brings all streams to a clean end: waiting streams wake up and end with `Closed`."""
        self._end = True
        for subscription in self._subscriptions.values():
            subscription.signal.set()

    def _remove(self, subscription: Subscription) -> None:
        self._subscriptions.pop((subscription.account, subscription.id), None)
        subscription.connection = None
        subscription.signal.set()

    def all(self, account: str | None = None) -> list[Subscription]:
        return [a for a in self._subscriptions.values() if account is None or a.account == account]

    def subscribe(self, account: str, type: Literal["streaming", "pull"], folder: frozenset[str], kinds: frozenset[EventKind], pull_timeout_min: int | None) -> Subscription:
        now = self._clock.now()
        own = sorted(self.all(account), key=lambda a: a.last)
        while len(own) >= self.MAX_SUBSCRIPTIONS_PER_ACCOUNT:
            free = [a for a in own if a.connection is None]
            if not free:
                raise EwsError(Code.ErrorExceededSubscriptionCount)
            own.remove(free[0])
            self._remove(free[0])
        subscription = Subscription(id=os.urandom(8), account=account, type=type, folder=folder, kinds=kinds, last=now)
        if type == "pull":
            subscription.pull_timeout_s = (pull_timeout_min or 30) * 60
            subscription.expiry = now + subscription.pull_timeout_s
        else:
            subscription.expiry = now + STREAM_IDLE_S
        self._subscriptions[(account, subscription.id)] = subscription
        return subscription

    def subscription_id(self, subscription: Subscription) -> str:
        return self._ids.subscription_id(subscription.account, self.epoch, subscription.id)

    def get(self, account: str, subscription_id: str, type: Literal["streaming", "pull"]) -> Subscription:
        try:
            epoch, raw = self._ids.read_subscription_id(account, subscription_id)
        except InvalidId:
            raise EwsError(Code.ErrorSubscriptionNotFound) from None
        if epoch != self.epoch:
            raise EwsError(Code.ErrorSubscriptionNotFound)
        subscription = self._subscriptions.get((account, raw))
        if subscription is None:
            raise EwsError(Code.ErrorSubscriptionNotFound)
        if subscription.type != type:
            raise EwsError(Code.ErrorInvalidSubscription)
        if subscription.type == "pull" and subscription.expiry < self._clock.now():
            self._subscriptions.pop((account, raw), None)
            raise EwsError(Code.ErrorExpiredSubscription)
        return subscription

    def cancel(self, account: str, subscription_id: str) -> None:
        try:
            epoch, raw = self._ids.read_subscription_id(account, subscription_id)
        except InvalidId:
            return
        subscription = self._subscriptions.get((account, raw)) if epoch == self.epoch else None
        if subscription is not None:
            self._remove(subscription)

    def report(self, account: str, folder: str, events: Sequence[Event]) -> None:
        """Synchronous, O(subscriptions), never awaits."""
        for subscription in self._subscriptions.values():
            if subscription.account != account or folder not in subscription.folder:
                continue
            for e in events:
                if e.kind not in subscription.kinds or folder in subscription.overflow:
                    continue
                if len(subscription.queue) >= self.MAX_QUEUE:
                    subscription.queue.clear()
                    subscription.overflow.add(folder)
                else:
                    subscription.queue.append(e)
            subscription.signal.set()

    def watched(self, account: str) -> frozenset[str]:
        folder: set[str] = set()
        for subscription in self._subscriptions.values():
            if subscription.account == account:
                folder |= subscription.folder
        return frozenset(folder)

    def streams_active(self, account: str) -> int:
        return len({a.connection for a in self._subscriptions.values() if a.account == account and a.connection is not None})

    def connect(self, subscriptions: Sequence[Subscription]) -> int:
        account = subscriptions[0].account if subscriptions else None
        if account is not None and self.streams_active(account) >= self.MAX_STREAMS_PER_ACCOUNT and not any(a.connection for a in subscriptions):
            raise EwsError(Code.ErrorExceededConnectionCount)
        self._generation += 1
        for subscription in subscriptions:
            old = subscription.connection
            subscription.connection = self._generation
            subscription.last = self._clock.now()
            if old is not None:
                subscription.signal.set()
        return self._generation

    def split(self, subscriptions: Sequence[Subscription], generation: int) -> None:
        for subscription in subscriptions:
            if subscription.connection == generation:
                subscription.connection = None
                subscription.last = self._clock.now()
                subscription.expiry = subscription.last + STREAM_IDLE_S

    def _empty(self, subscription: Subscription) -> list[Event]:
        events = list(subscription.queue)
        subscription.queue.clear()
        for folder in sorted(subscription.overflow):
            events.append(Event(EventKind.MODIFIED, folder, None, self._clock.now()))
        subscription.overflow.clear()
        subscription.signal.clear()
        return events

    async def wait(self, subscriptions: Sequence[Subscription], generation: int, until: float) -> list[tuple[Subscription, list[Event]]] | Displaced:
        while True:
            if any(a.connection != generation for a in subscriptions):
                return DISPLACED
            if self._end:
                return []
            hit = [(a, self._empty(a)) for a in subscriptions if a.queue or a.overflow]
            if hit:
                return hit
            rest = until - self._clock.now()
            if rest <= 0:
                return []
            waiter = [asyncio.ensure_future(a.signal.wait()) for a in subscriptions]
            timer = asyncio.ensure_future(self._clock.sleep(rest))
            try:
                done, _ = await asyncio.wait([*waiter, timer], return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in [*waiter, timer]:
                    t.cancel()
            if timer in done and not any(w in done for w in waiter):
                if any(a.connection != generation for a in subscriptions):
                    return DISPLACED
                return [(a, self._empty(a)) for a in subscriptions if a.queue or a.overflow]
            for a in subscriptions:
                a.signal.clear() if not (a.queue or a.overflow) and a.connection == generation else None

    def pull(self, subscription: Subscription, watermark: int | None) -> tuple[list[tuple[int, Event]], bool]:
        now = self._clock.now()
        if watermark is not None:
            if watermark > subscription.sequence:
                raise EwsError(Code.ErrorInvalidWatermark)
            subscription.unconfirmed = [(f, e) for f, e in subscription.unconfirmed if f > watermark]
            subscription.acknowledged = max(subscription.acknowledged, watermark)
        for e in self._empty(subscription):
            subscription.sequence += 1
            subscription.unconfirmed.append((subscription.sequence, e))
        too_much = len(subscription.unconfirmed) - self.MAX_PULL_UNCONFIRMED
        if too_much > 0:
            discarded = subscription.unconfirmed[:too_much]
            del subscription.unconfirmed[:too_much]
            for folder in sorted({e.folder for _, e in discarded}):
                subscription.sequence += 1
                subscription.unconfirmed.append((subscription.sequence, Event(EventKind.MODIFIED, folder, None, now)))
        subscription.expiry = now + (subscription.pull_timeout_s or 1800)
        subscription.last = now
        block = subscription.unconfirmed[: self.PULL_BLOCK]
        return block, len(subscription.unconfirmed) > len(block)

    def clean_up(self) -> None:
        now = self._clock.now()
        for key, subscription in list(self._subscriptions.items()):
            if subscription.type == "pull" and subscription.expiry < now:
                del self._subscriptions[key]
            elif subscription.type == "streaming" and subscription.connection is None and subscription.expiry < now:
                del self._subscriptions[key]
