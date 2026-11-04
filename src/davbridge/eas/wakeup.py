"""Waker for waiting Ping/Sync-Wait. RAM only, no threads: report() is synchronous and never waits."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Literal

from ..clock import Clock


@dataclass(slots=True)
class _Waiter:
    account: str
    device: str
    folder: frozenset[str]
    event: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str | None = None


class Waker:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._waiter: dict[tuple[str, str], _Waiter] = {}
        self._ended = False

    def report(self, account: str, folder: str) -> None:
        for w in list(self._waiter.values()):
            if w.account == account and folder in w.folder and w.reason is None:
                w.reason = "signal"
                w.event.set()

    def watched(self, account: str) -> frozenset[str]:
        result: set[str] = set()
        for w in self._waiter.values():
            if w.account == account:
                result |= w.folder
        return frozenset(result)

    def accounts(self) -> frozenset[str]:
        return frozenset(w.account for w in self._waiter.values())

    def devices(self, account: str) -> frozenset[str]:
        return frozenset(g for (k, g) in self._waiter if k == account)

    def count(self, account: str | None = None) -> int:
        return sum(1 for w in self._waiter.values() if account is None or w.account == account)

    def replace(self, account: str, device: str) -> None:
        w = self._waiter.get((account, device))
        if w is not None and w.reason is None:
            w.reason = "replaced"
            w.event.set()

    def end(self) -> None:
        self._ended = True
        for w in self._waiter.values():
            if w.reason is None:
                w.reason = "end"
            w.event.set()

    async def wait(self, account: str, device: str, folder: Collection[str], until: float, tick_s: float | None = None) -> Literal["signal", "timeout", "replaced", "end", "poll"]:
        """Waits for a signal, expiry, replacement by a newer request of the same device, or shutdown.
        With `tick_s` the wait ends after at most that time with "poll" (periodic check)."""
        if self._ended:
            return "end"
        self.replace(account, device)
        w = _Waiter(account, device, frozenset(folder))
        self._waiter[(account, device)] = w
        try:
            rest = until - self._clock.now()
            deadline = min(rest, tick_s) if tick_s is not None else rest
            if deadline <= 0:
                return "timeout"
            try:
                await asyncio.wait_for(w.event.wait(), timeout=deadline)
            except asyncio.TimeoutError:
                return "timeout" if tick_s is None or rest <= tick_s else "poll"
            return w.reason or "signal"  # type: ignore[return-value]
        finally:
            if self._waiter.get((account, device)) is w:
                del self._waiter[(account, device)]
