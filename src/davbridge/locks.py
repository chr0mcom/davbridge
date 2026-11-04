"""Write locks: they only serialize our own retries and never replace If-Match."""

from __future__ import annotations

import asyncio
import weakref
from contextlib import asynccontextmanager
from typing import AsyncIterator


class Locks:
    def __init__(self) -> None:
        self._locks: weakref.WeakValueDictionary[tuple, asyncio.Lock] = weakref.WeakValueDictionary()

    def _lock(self, *key: object) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @asynccontextmanager
    async def element(self, account: str, cal: str, href: str) -> AsyncIterator[None]:
        async with self._lock("e", account, cal, href):
            yield

    @asynccontextmanager
    async def creation(self, account: str, key: object) -> AsyncIterator[None]:
        async with self._lock("a", account, key):
            yield

    @asynccontextmanager
    async def eas_sync(self, account: str, device: str, folder: str) -> AsyncIterator[None]:
        async with self._lock("s", account, device, folder):
            yield
