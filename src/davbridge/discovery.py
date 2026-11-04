"""Calendar autodiscovery for users without a `[[users.calendars]]` list (`calendar_mode = "auto"`, pattern mode).

Runs only after a successful CalDAV login. The CalDAV client enforces the boundaries (principal, calendar home, origin, no redirects);
this module turns the candidates into `CalendarConfig` entries, picks the default calendar and caches the metadata for five minutes.
Nothing but names, paths and rights is cached, never calendar content.
"""

from __future__ import annotations

import dataclasses
from typing import Literal

from .caldav import Caldav, Credentials, DiscoveredCalendar
from .clock import Clock
from .config import Account, CalendarConfig, Config
from .identifiers import IdService

CACHE_SECONDS = 300
FAILURE_SECONDS = 60


class DiscoveryError(Exception):
    """The calendars of the user cannot be turned into a usable hierarchy (reason: `no-calendar` or `no-default-calendar`)."""

    def __init__(self, reason: Literal["no-calendar", "no-default-calendar"]) -> None:
        super().__init__(reason)
        self.reason = reason


def choose_default(candidates: list[DiscoveredCalendar], keys: dict[str, str], rule: str, read_only_owners: frozenset[str]) -> str:
    """The default calendar is never guessed: `only` needs exactly one candidate, `first-writable` is an explicit opt-in,
    anything else must name a candidate exactly (by path segment or by its derived id). Returns the path segment."""
    if not candidates:
        raise DiscoveryError("no-calendar")
    if rule == "only":
        if len(candidates) != 1:
            raise DiscoveryError("no-default-calendar")
        return candidates[0].uri
    if rule == "first-writable":
        for c in candidates:
            if c.writable and c.owner not in read_only_owners:
                return c.uri
        raise DiscoveryError("no-default-calendar")
    for c in candidates:
        if rule in (c.uri, keys[c.uri]):
            return c.uri
    raise DiscoveryError("no-default-calendar")


class Discovery:
    def __init__(self, config: Config, caldav: Caldav, ids: IdService, clock: Clock) -> None:
        self._config = config
        self._caldav = caldav
        self._ids = ids
        self._clock = clock
        self._cache: dict[str, tuple[float, tuple[CalendarConfig, ...] | DiscoveryError]] = {}

    async def resolve(self, account: Account, credentials: Credentials) -> Account:
        """The account with its calendars filled in. Accounts with an explicit calendar list are returned unchanged."""
        if not account.auto:
            return account
        entry = self._cache.get(account.name)
        now = self._clock.now()
        if entry is None or entry[0] <= now:
            try:
                entry = (now + CACHE_SECONDS, await self._discover(account, credentials))
            except DiscoveryError as e:
                entry = (now + FAILURE_SECONDS, e)
            self._cache[account.name] = entry
        if isinstance(entry[1], DiscoveryError):
            raise entry[1]
        return dataclasses.replace(account, calendar=entry[1])

    async def _discover(self, account: Account, credentials: Credentials) -> tuple[CalendarConfig, ...]:
        dynamic = self._config.dynamic
        candidates = await self._caldav.discover_calendars(credentials, dynamic.include_shared)
        keys = {c.uri: self._ids.calendar_key(account.name, c.uri) for c in candidates}
        default = choose_default(candidates, keys, dynamic.default_calendar, self._config.read_only_owners)
        calendars = [
            CalendarConfig(
                uri=c.uri,
                key=keys[c.uri],
                display_name=c.display_name,
                default=c.uri == default,
                write="never" if c.owner in self._config.read_only_owners or not c.writable else "caldav",
                owner=c.owner or account.name,
            )
            for c in candidates
        ]
        return tuple(calendars)

    def discard(self, account: str) -> None:
        self._cache.pop(account, None)
