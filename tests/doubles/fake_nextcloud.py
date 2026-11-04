"""In-memory double for the `Caldav` protocol. Reproduces the behaviour observed on a real CalDAV server (integration tests):
an ETag change per modification, sync tokens `http://sabre.io/ns/sync/<n>` with truncation, DELETE+If-Match on a missing object => 412,
PUT into a read-only calendar => FORBIDDEN, UID conflict in another file. All data is synthetic."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from davbridge.caldav import CaldavError, CaldavKind, CalendarInfo, Credentials, DavObject, Delta, DiscoveredCalendar
from davbridge.config import CalendarConfig

TOKEN_PREFIX = "http://sabre.io/ns/sync/"


def ics(uid: str, title: str = "Synthetic", start: str = "20261201T090000Z", end: str = "20261201T100000Z", extra: str = "") -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//en", "BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20261101T120000Z", f"DTSTART:{start}", f"DTEND:{end}", f"SUMMARY:{title}"]
    if extra:
        lines.append(extra)
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines)


def series_ics(uid: str, title: str = "Series") -> str:
    return ics(uid, title, extra="RRULE:FREQ=WEEKLY;COUNT=4")


def vtodo_ics(uid: str) -> str:
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//en", "BEGIN:VTODO", f"UID:{uid}", "DTSTAMP:20261101T120000Z", "SUMMARY:Task", "END:VTODO", "END:VCALENDAR", ""])


@dataclass
class FakeCalendar:
    writable: bool = True
    display_name: str = "Fake"
    dav_objects: dict[str, tuple[str, str]] = field(default_factory=dict)  # href -> (etag, ics)
    hidden: set[str] = field(default_factory=set)  # visible in sync, missing in multiget/GET
    changes: list[tuple[int, str]] = field(default_factory=list)  # (n, href)
    n: int = 1
    min_token: int = 0
    counter: int = 0
    shared: bool = False
    owner: str = ""
    hidden_from_discovery: bool = False

    def token(self) -> str:
        return f"{TOKEN_PREFIX}{self.n}"

    def _etag(self, ics_text: str) -> str:
        self.counter += 1
        return '"' + hashlib.md5(f"{self.counter}|{ics_text}".encode()).hexdigest() + '"'

    def write(self, href: str, ics_text: str) -> str:
        etag = self._etag(ics_text)
        self.dav_objects[href] = (etag, ics_text)
        self.n += 1
        self.changes.append((self.n, href))
        return etag

    def remove(self, href: str) -> None:
        self.dav_objects.pop(href, None)
        self.n += 1
        self.changes.append((self.n, href))


class FakeNextcloud:
    """An account-spanning store: (user, cal.uri) => FakeCalendar. No password per call, only `check_access`."""

    def __init__(self, passwords: dict[str, str] | None = None) -> None:
        self.passwords = dict(passwords or {"test-user": "pw-test-1234"})
        self.calendar: dict[tuple[str, str], FakeCalendar] = {}
        self.access_calls = 0
        self.canonical_calls = 0
        self.canonical_ids: dict[str, str] = {}  # lower-case login -> the user id the server uses; the server then ignores the case of the login
        self.discover_calls = 0
        self.sync_calls: list[str | None] = []
        self.multiget_calls = 0
        self.failure: CaldavError | None = None
        self.put_without_etag = False
        self.auth_error_on_sync = False

    # ------------------------------------------------------------------ test helpers
    def cal(self, user: str, uri: str, *, writable: bool = True) -> FakeCalendar:
        k = self.calendar.setdefault((user, uri), FakeCalendar(writable=writable))
        k.writable = writable
        return k

    def _k(self, z: Credentials, cal: CalendarConfig) -> FakeCalendar:
        k = self.calendar.get((z.user, cal.uri))
        if k is None:
            raise CaldavError(CaldavKind.NOT_FOUND, 404)
        return k

    def _check_failure(self) -> None:
        if self.failure is not None:
            raise self.failure

    # ------------------------------------------------------------------ Protocol
    async def check_access(self, z: Credentials) -> bool:
        self.access_calls += 1
        self._check_failure()
        return self.passwords.get(self.canonical_ids.get(z.user.lower(), z.user)) == z.password

    async def canonical_user(self, z: Credentials) -> str | None:
        self.canonical_calls += 1
        self._check_failure()
        return self.canonical_ids.get(z.user.lower())

    async def discover_calendars(self, z: Credentials, include_shared: bool) -> list[DiscoveredCalendar]:
        self.discover_calls += 1
        self._check_failure()
        found = [
            DiscoveredCalendar(uri, k.display_name, k.writable, k.shared, k.owner or z.user)
            for (user, uri), k in sorted(self.calendar.items())
            if user == self.canonical_ids.get(z.user.lower(), z.user) and not k.hidden_from_discovery and (include_shared or not k.shared)
        ]
        return found

    async def calendar_info(self, z: Credentials, cal: CalendarConfig) -> CalendarInfo:
        self._check_failure()
        k = self._k(z, cal)
        return CalendarInfo(k.display_name, k.token(), k.writable, frozenset({"VEVENT"}))

    async def sync(self, z: Credentials, cal: CalendarConfig, token: str | None) -> Delta:
        self._check_failure()
        self.sync_calls.append(token)
        if self.auth_error_on_sync:
            raise CaldavError(CaldavKind.AUTH, 401)
        k = self._k(z, cal)
        if not token:
            return Delta(k.token(), {h: e for h, (e, _) in k.dav_objects.items()} | {h: '"hidden"' for h in k.hidden if h not in k.dav_objects}, frozenset(), True)
        hit = re.fullmatch(re.escape(TOKEN_PREFIX) + r"(\d+)", token)
        if not hit:
            raise CaldavError(CaldavKind.TOKEN_INVALID, 403)
        n = int(hit.group(1))
        if n < k.min_token or n > k.n:
            raise CaldavError(CaldavKind.TOKEN_INVALID, 403)
        changed: dict[str, str] = {}
        deleted: set[str] = set()
        for number, href in k.changes:
            if number <= n:
                continue
            if href in k.dav_objects:
                changed[href] = k.dav_objects[href][0]
                deleted.discard(href)
            elif href in k.hidden:
                changed[href] = '"hidden"'
            else:
                deleted.add(href)
                changed.pop(href, None)
        return Delta(k.token(), changed, frozenset(deleted), False)

    async def get_several(self, z: Credentials, cal: CalendarConfig, hrefs: Sequence[str]) -> list[DavObject]:
        self._check_failure()
        self.multiget_calls += 1
        k = self._k(z, cal)
        return [DavObject(h, k.dav_objects[h][0], k.dav_objects[h][1]) for h in hrefs if h in k.dav_objects and h not in k.hidden]

    async def get(self, z: Credentials, cal: CalendarConfig, href: str) -> DavObject | None:
        self._check_failure()
        k = self._k(z, cal)
        if href not in k.dav_objects or href in k.hidden:
            return None
        return DavObject(href, k.dav_objects[href][0], k.dav_objects[href][1])

    async def time_range(self, z: Credentials, cal: CalendarConfig, since: datetime, until: datetime) -> list[DavObject]:
        self._check_failure()
        k = self._k(z, cal)
        return [DavObject(h, e, i) for h, (e, i) in sorted(k.dav_objects.items()) if h not in k.hidden]

    async def search_uid(self, z: Credentials, cal: CalendarConfig, uid: str) -> DavObject | None:
        self._check_failure()
        k = self._k(z, cal)
        for h, (e, i) in k.dav_objects.items():
            if f"UID:{uid}" in i:
                return DavObject(h, e, i)
        return None

    def _uid(self, text: str) -> str:
        hit = re.search(r"^UID:(.+)$", text, re.M)
        return hit.group(1).strip() if hit else ""

    async def create(self, z: Credentials, cal: CalendarConfig, href: str, text: str) -> str | None:
        self._check_failure()
        k = self._k(z, cal)
        if not k.writable:
            raise CaldavError(CaldavKind.FORBIDDEN, 404)
        if href in k.dav_objects:
            raise CaldavError(CaldavKind.CONFLICT, 412)
        uid = self._uid(text)
        if any(self._uid(i) == uid for _, i in k.dav_objects.values()):
            raise CaldavError(CaldavKind.UID_CONFLICT, 409)
        etag = k.write(href, text)
        return None if self.put_without_etag else etag

    async def change(self, z: Credentials, cal: CalendarConfig, href: str, text: str, if_match: str) -> str | None:
        self._check_failure()
        k = self._k(z, cal)
        if not k.writable:
            raise CaldavError(CaldavKind.FORBIDDEN, 404)
        if href not in k.dav_objects or k.dav_objects[href][0] != if_match:
            raise CaldavError(CaldavKind.CONFLICT, 412)
        etag = k.write(href, text)
        return None if self.put_without_etag else etag

    async def delete(self, z: Credentials, cal: CalendarConfig, href: str, if_match: str) -> bool:
        self._check_failure()
        k = self._k(z, cal)
        if not k.writable:
            raise CaldavError(CaldavKind.FORBIDDEN, 404)
        if href not in k.dav_objects or k.dav_objects[href][0] != if_match:
            raise CaldavError(CaldavKind.CONFLICT, 412)
        k.remove(href)
        return True
