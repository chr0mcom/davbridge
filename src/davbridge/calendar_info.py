"""Short-lived cache for CalendarInfo (permissions, display name): one PROPFIND per calendar and minute instead of one per request."""

from __future__ import annotations

from .caldav import Caldav, CalendarInfo, Credentials
from .clock import Clock
from .config import CalendarConfig

TTL_S = 60


class CalendarInfoCache:
    def __init__(self, caldav: Caldav, clock: Clock) -> None:
        self._caldav = caldav
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[float, CalendarInfo]] = {}

    async def get(self, z: Credentials, cal: CalendarConfig) -> CalendarInfo:
        key = (z.user, cal.key)
        entry = self._cache.get(key)
        now = self._clock.now()
        if entry and entry[0] > now:
            return entry[1]
        info = await self._caldav.calendar_info(z, cal)
        self._cache[key] = (now + TTL_S, info)
        return info

    def discard(self, account: str) -> None:
        for k in [k for k in self._cache if k[0] == account]:
            del self._cache[k]
