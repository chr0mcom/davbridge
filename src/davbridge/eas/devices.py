"""Facade over the store for SyncKey chains per (account, device, folder)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..identifiers import InvalidId
from ..store import EasSync, Store

if TYPE_CHECKING:
    from .commands import EasContext

Mode = Literal["new", "normal", "repeat", "invalid"]


@dataclass(frozen=True, slots=True)
class Situation:
    mode: Mode
    state: EasSync | None
    reason: str | None = None


class Devices:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._last_sync: dict[tuple[str, str], bytes] = {}
        self._ping: dict[tuple[str, str], tuple[int, tuple[tuple[str, str], ...]]] = {}
        self._deletions: dict[str, list[float]] = {}

    async def load(self, ctx: "EasContext", folder: str, sync_key: str, raw_hash: bytes) -> Situation:
        if sync_key == "0":
            return Situation("new", None)
        try:
            counter = ctx.ids.read_eas_sync_key(ctx.account.name, ctx.device, folder, sync_key)
        except InvalidId:
            return Situation("invalid", None, "signature")
        state = await self._store.eas_sync_read(ctx.account.name, ctx.device, folder)
        if state is None:
            return Situation("invalid", None, "counter")
        if counter == state.counter:
            return Situation("normal", state)
        if counter == state.counter - 1 and state.plan is not None:
            if state.request_hash == raw_hash:
                return Situation("repeat", state)
            return Situation("invalid", state, "body")
        return Situation("invalid", state, "counter")

    async def store(self, new: EasSync, *, expected: int | None) -> bool:
        return await self._store.eas_sync_write(new, expected=expected)

    def remember_sync(self, account: str, device: str, raw: bytes) -> None:
        """Last Sync request without Commands (<= 8 KiB) for the empty Sync; never with appointment content."""
        if len(raw) <= 8192:
            self._last_sync[(account, device)] = raw

    def last_sync(self, account: str, device: str) -> bytes | None:
        return self._last_sync.get((account, device))

    def remember_ping(self, account: str, device: str, heartbeat: int, folder: tuple[tuple[str, str], ...]) -> None:
        self._ping[(account, device)] = (heartbeat, folder)

    def last_ping(self, account: str, device: str) -> tuple[int, tuple[tuple[str, str], ...]] | None:
        return self._ping.get((account, device))

    def options(self, old: Mapping | None, new: Mapping | None) -> Mapping:
        """Options are "sticky": new values replace old ones, remembered fields such as `ghost` stay."""
        return {**dict(old or {}), **dict(new or {})}

    def delete_allowed(self, account: str, known: int, now: float, minimum: int = 5) -> bool:
        """EAS delete brake: at most max(5, 10 % of the known items) client deletions per account and hour."""
        times = [t for t in self._deletions.get(account, []) if now - t < 3600]
        self._deletions[account] = times
        return len(times) < max(minimum, known // 10)

    def remember_deletion(self, account: str, now: float) -> None:
        self._deletions.setdefault(account, []).append(now)
