"""CalDAV client for the backing server. The only module that speaks HTTP to it. No EWS terms, no URLs or bodies in errors."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from http.cookiejar import CookiePolicy
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit

import httpx
from lxml import etree

from .config import USERNAME_PATTERN, CalendarConfig, valid_calendar_segment

FILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@%+-]{0,199}\.ics$")
DAV_ROOT = "/remote.php/dav"
D = "DAV:"
C = "urn:ietf:params:xml:ns:caldav"
OC = "http://owncloud.org/ns"
NSMAP = {"d": D, "c": C, "oc": OC}
BLOCK = 100
MAX_FOLLOW = 20
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
PARSE_OFF_LOOP_BYTES = 256 * 1024


def new_parser() -> etree.XMLParser:
    """A hardened parser of its own. lxml serialises every parse that uses the same parser object, so a parser shared between the event loop
    and the worker threads would let one large answer block every small one behind it."""
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False, huge_tree=False, remove_comments=True)


@dataclass(frozen=True, slots=True)
class Credentials:
    user: str
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class DavObject:
    href: str
    etag: str
    ics: str | None


@dataclass(frozen=True, slots=True)
class Delta:
    token: str
    changed: Mapping[str, str]
    deleted: frozenset[str]
    full: bool


@dataclass(frozen=True, slots=True)
class CalendarInfo:
    display_name: str
    sync_token: str
    writable: bool
    components: frozenset[str]


@dataclass(frozen=True, slots=True)
class DiscoveredCalendar:
    """One calendar collection found in the user's calendar home. `uri` is the single path segment below the home."""

    uri: str
    display_name: str
    writable: bool
    shared: bool
    owner: str


class CaldavKind(StrEnum):
    AUTH = "auth"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    TOKEN_INVALID = "token_invalid"
    UID_CONFLICT = "uid_conflict"
    QUOTA = "quota"
    BUSY = "busy"
    NETWORK = "network"
    UNEXPECTED = "unexpected"


class CaldavError(Exception):
    def __init__(self, kind: CaldavKind, status: int | None = None) -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.status = status


class Caldav(Protocol):
    async def check_access(self, z: Credentials) -> bool: ...
    async def canonical_user(self, z: Credentials) -> str | None: ...
    async def discover_calendars(self, z: Credentials, include_shared: bool) -> list[DiscoveredCalendar]: ...
    async def calendar_info(self, z: Credentials, cal: CalendarConfig) -> CalendarInfo: ...
    async def sync(self, z: Credentials, cal: CalendarConfig, token: str | None) -> Delta: ...
    async def get_several(self, z: Credentials, cal: CalendarConfig, hrefs: Sequence[str]) -> list[DavObject]: ...
    async def get(self, z: Credentials, cal: CalendarConfig, href: str) -> DavObject | None: ...
    async def time_range(self, z: Credentials, cal: CalendarConfig, since: datetime, until: datetime) -> list[DavObject]: ...
    async def search_uid(self, z: Credentials, cal: CalendarConfig, uid: str) -> DavObject | None: ...
    async def create(self, z: Credentials, cal: CalendarConfig, href: str, ics: str) -> str | None: ...
    async def change(self, z: Credentials, cal: CalendarConfig, href: str, ics: str, if_match: str) -> str | None: ...
    async def delete(self, z: Credentials, cal: CalendarConfig, href: str, if_match: str) -> bool: ...


def normalize_etag(etag: str) -> str:
    return re.sub(r'-(gzip|br|deflate)"$', '"', etag.strip())


def token_number(token: str | None) -> int | None:
    hit = re.search(r"/(\d+)$", token or "")
    return int(hit.group(1)) if hit else None


def check_filename(href: str) -> str:
    if not FILE_PATTERN.match(href):
        raise ValueError("invalid file name")
    return href


def _el(tag: str, nsmap: Mapping[str, str] = NSMAP, **attr: str) -> etree._Element:
    prefix, _, local = tag.partition(":")
    return etree.Element("{%s}%s" % (nsmap[prefix], local), nsmap=dict(nsmap), **attr)


def _sub(parents: etree._Element, tag: str, text: str | None = None) -> etree._Element:
    prefix, _, local = tag.partition(":")
    kind = etree.SubElement(parents, "{%s}%s" % (NSMAP[prefix], local))
    if text is not None:
        kind.text = text
    return kind


def _bytes(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def _status_from(text: str | None) -> int:
    parts = (text or "").split()
    return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0


def _error(status: int, body: bytes = b"") -> CaldavError:
    if b"no-uid-conflict" in body:
        return CaldavError(CaldavKind.UID_CONFLICT, status)
    if status == 401:
        return CaldavError(CaldavKind.AUTH, status)
    if status == 403:
        if b"no-uid-conflict" in body:
            return CaldavError(CaldavKind.UID_CONFLICT, status)
        if b"valid-sync-token" in body:
            return CaldavError(CaldavKind.TOKEN_INVALID, status)
        return CaldavError(CaldavKind.FORBIDDEN, status)
    if status in (404, 410):
        return CaldavError(CaldavKind.NOT_FOUND, status)
    if status in (409, 412):
        return CaldavError(CaldavKind.CONFLICT, status)
    if status == 507:
        return CaldavError(CaldavKind.QUOTA, status)
    if status in (429, 502, 503, 504):
        return CaldavError(CaldavKind.BUSY, status)
    return CaldavError(CaldavKind.UNEXPECTED, status)


class NoCookies(CookiePolicy):
    """After Basic auth the server sets a session cookie. It would mask wrong passwords and account switches."""

    netscape = True
    rfc2965 = hide_cookie2 = False

    def set_ok(self, cookie, request) -> bool:
        return False

    def return_ok(self, cookie, request) -> bool:
        return False

    def domain_return_ok(self, domain, request) -> bool:
        return False

    def path_return_ok(self, path, request) -> bool:
        return False


def _parse(content: bytes) -> etree._Element:
    return etree.fromstring(content, new_parser())


class HttpCaldav:
    def __init__(self, base: str, http: httpx.AsyncClient, limit_per_account: int = 4) -> None:
        self._base = base.rstrip("/")
        parts = urlsplit(self._base)
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._prefix = parts.path.split(DAV_ROOT)[0].rstrip("/")
        self._http = http
        self._http.cookies.jar.set_policy(NoCookies())
        self._http.cookies.clear()
        self._limit = limit_per_account
        self._sem: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(limit_per_account))
        self.foreign_paths = 0

    def _path(self, z: Credentials, cal: CalendarConfig) -> str:
        return f"{self._prefix}{DAV_ROOT}/calendars/{quote(z.user, safe='')}/{quote(cal.uri, safe='@_-.%+')}/"

    def _url(self, z: Credentials, cal: CalendarConfig, href: str = "") -> str:
        return self._origin + self._path(z, cal) + (quote(check_filename(href)) if href else "")

    async def _request(
        self, z: Credentials, method: str, url: str, *, body: bytes | None = None, header: Mapping[str, str] | None = None, expected: Sequence[int] = (), no_redirect: bool = False
    ) -> httpx.Response:
        # identity: Apache appends "-gzip" to the ETag when it compresses; such an ETag never matches If-Match.
        header_lines = {"User-Agent": "davbridge/1", "Accept-Encoding": "identity"}
        if body is not None:
            header_lines["Content-Type"] = "application/xml; charset=utf-8" if method in ("PROPFIND", "REPORT") else "text/calendar; charset=utf-8"
        header_lines.update(header or {})
        try:
            async with self._sem[z.user]:
                async with self._http.stream(method, url, content=body, headers=header_lines, auth=httpx.BasicAuth(z.user, z.password)) as raw:
                    if no_redirect and (raw.history or raw.is_redirect):
                        raise CaldavError(CaldavKind.UNEXPECTED, raw.status_code)
                    parts: list[bytes] = []
                    total = 0
                    async for chunk in raw.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_RESPONSE_BYTES:
                            raise CaldavError(CaldavKind.UNEXPECTED, raw.status_code)
                        parts.append(chunk)
                    header_raw = [(k, v) for k, v in raw.headers.multi_items() if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
                    response = httpx.Response(raw.status_code, headers=header_raw, content=b"".join(parts), request=raw.request)
        except (httpx.TimeoutException, httpx.PoolTimeout):
            raise CaldavError(CaldavKind.BUSY, None) from None
        except httpx.HTTPError:
            raise CaldavError(CaldavKind.NETWORK, None) from None
        if expected and response.status_code not in expected:
            raise _error(response.status_code, response.content[:2000])
        return response

    async def _multistatus(self, response: httpx.Response) -> etree._Element:
        """Parses a multistatus answer. Bodies above PARSE_OFF_LOOP_BYTES are parsed in a worker thread so that one large synchronisation
        answer cannot stall every other client on the single event-loop thread (libxml2 releases the GIL while it parses)."""
        content = response.content
        try:
            if len(content) > PARSE_OFF_LOOP_BYTES:
                return await asyncio.to_thread(_parse, content)
            return _parse(content)
        except etree.XMLSyntaxError:
            raise CaldavError(CaldavKind.UNEXPECTED, response.status_code) from None

    def _file_from_href(self, href: str, path: str) -> str | None:
        raw = unquote(href)
        if "://" in raw:
            rest = raw.split("://", 1)[1]
            if "/" not in rest:
                self.foreign_paths += 1
                return None
            raw = "/" + rest.split("/", 1)[1]
        if not raw.startswith(unquote(path)):
            self.foreign_paths += 1
            return None
        name = raw[len(unquote(path)) :]
        if not FILE_PATTERN.match(name):
            if name:
                self.foreign_paths += 1
            return None
        return name

    async def check_access(self, z: Credentials) -> bool:
        body = _bytes(self._propfind_body("d:resourcetype"))
        response = await self._request(z, "PROPFIND", f"{self._origin}{self._prefix}{DAV_ROOT}/principals/users/{quote(z.user, safe='')}/", body=body, header={"Depth": "0"})
        if response.status_code == 401:
            return False
        if response.status_code == 207:
            return True
        raise _error(response.status_code)

    async def canonical_user(self, z: Credentials) -> str | None:
        """The user id the server itself uses for the signed-in user (the last segment of `current-user-principal`), only if it equals the
        login name apart from its case. None for anything else: the caller then keeps the name as typed."""
        principal_path = f"{self._prefix}{DAV_ROOT}/principals/users/{quote(z.user, safe='')}/"
        response = await self._request(
            z, "PROPFIND", self._origin + principal_path, body=_bytes(self._propfind_body("d:current-user-principal")), header={"Depth": "0"}, expected=(207,), no_redirect=True
        )
        path = self._canonical_path((await self._multistatus(response)).findtext(".//d:current-user-principal/d:href", None, NSMAP))
        head = f"{self._prefix}{DAV_ROOT}/principals/users/"
        if path is None or not path.startswith(head):
            return None
        name = path[len(head) :].rstrip("/")
        if not name or "/" in name or name.lower() != z.user.lower() or not USERNAME_PATTERN.fullmatch(name):
            return None
        return name

    def _propfind_body(self, *props: str) -> etree._Element:
        root = _el("d:propfind")
        prop = _sub(root, "d:prop")
        for p in props:
            _sub(prop, p)
        return root

    def _canonical_path(self, href: str | None) -> str | None:
        """Path of a DAV href, decoded exactly once. None for anything that is not a plain path on the configured origin
        (other scheme, host or port, query, fragment, `.`/`..` segments, empty segments, backslashes or a second layer of percent encoding)."""
        if not href or not href.strip():
            return None
        parts = urlsplit(href.strip())
        origin = urlsplit(self._base)
        if parts.scheme or parts.netloc:
            if parts.scheme.lower() != origin.scheme.lower() or (parts.hostname or "").lower() != (origin.hostname or "").lower() or parts.port != origin.port:
                return None
        if parts.query or parts.fragment or not parts.path.startswith("/"):
            return None
        path = unquote(parts.path)
        if re.search(r"%[0-9A-Fa-f]{2}", path) or "\\" in path or "\0" in path or "//" in path:
            return None
        if any(segment in (".", "..") for segment in path.split("/")):
            return None
        return path

    @staticmethod
    def _slash(path: str) -> str:
        return path if path.endswith("/") else path + "/"

    async def discover_calendars(self, z: Credentials, include_shared: bool) -> list[DiscoveredCalendar]:
        """Two requests, both bound to the authenticated user: the principal (current-user-principal, calendar-home-set), then one
        `Depth: 1` PROPFIND on that calendar home. Only VEVENT calendars the user can read are returned; shared calendars only on request."""
        principal_path = f"{self._prefix}{DAV_ROOT}/principals/users/{quote(z.user, safe='')}/"
        home_path = f"{self._prefix}{DAV_ROOT}/calendars/{quote(z.user, safe='')}/"
        first = await self._request(
            z, "PROPFIND", self._origin + principal_path, body=_bytes(self._propfind_body("d:current-user-principal", "c:calendar-home-set")), header={"Depth": "0"}, expected=(207,), no_redirect=True
        )
        root = await self._multistatus(first)
        principal = self._canonical_path(root.findtext(".//d:current-user-principal/d:href", None, NSMAP))
        home = self._canonical_path(root.findtext(".//c:calendar-home-set/d:href", None, NSMAP))
        expected_principal, expected_home = unquote(principal_path), unquote(home_path)
        if principal is None or home is None or self._slash(principal) != expected_principal or self._slash(home) != expected_home:
            raise CaldavError(CaldavKind.UNEXPECTED, first.status_code)
        props = ("d:resourcetype", "d:displayname", "c:supported-calendar-component-set", "d:current-user-privilege-set", "d:owner", "oc:owner-principal", "oc:read-only")
        second = await self._request(z, "PROPFIND", self._origin + home_path, body=_bytes(self._propfind_body(*props)), header={"Depth": "1"}, expected=(207,), no_redirect=True)
        found: list[DiscoveredCalendar] = []
        for r in (await self._multistatus(second)).findall("d:response", NSMAP):
            path = self._canonical_path(r.findtext("d:href", "", NSMAP))
            if path is None or not path.startswith(expected_home):
                self.foreign_paths += 1
                continue
            segment = path[len(expected_home) :].rstrip("/")
            if not segment:
                continue
            if "/" in segment or not valid_calendar_segment(segment):
                self.foreign_paths += 1
                continue
            ok = [ps for ps in r.findall("d:propstat", NSMAP) if _status_from(ps.findtext("d:status", None, NSMAP)) == 200]
            if not any(ps.find("d:prop/d:resourcetype/c:calendar", NSMAP) is not None for ps in ok):
                continue

            def prop(path_: str) -> etree._Element | None:
                return next((e for ps in ok if (e := ps.find(path_, NSMAP)) is not None), None)

            components_node = prop("d:prop/c:supported-calendar-component-set")
            privileges_node = prop("d:prop/d:current-user-privilege-set")
            if components_node is None or privileges_node is None or "VEVENT" not in {c.get("name", "") for c in components_node.findall("c:comp", NSMAP)}:
                continue
            privileges = {etree.QName(n).localname for n in privileges_node.findall("d:privilege/*", NSMAP)}
            if not privileges & {"read", "all"}:
                continue
            owner_node = prop("d:prop/d:owner/d:href")
            owner_raw = owner_node.text if owner_node is not None else None
            if not owner_raw:
                alternative = prop("d:prop/oc:owner-principal")
                owner_raw = (self._prefix + DAV_ROOT + "/" + alternative.text.strip().lstrip("/")) if alternative is not None and alternative.text else None
            owner_path = self._canonical_path(owner_raw)
            owner = owner_path.rstrip("/").rsplit("/", 1)[-1] if owner_path else ""
            own = owner_path is not None and self._slash(owner_path) == expected_principal
            shared = (owner_path is not None and not own) or "_shared_by_" in segment
            if shared and not include_shared:
                continue
            if not own and not shared and not include_shared:
                continue
            hint = prop("d:prop/oc:read-only")
            read_only = hint is not None and (hint.text or "").strip() in ("1", "true")
            writable = bool(privileges & {"write", "write-content", "all"}) and not read_only
            name_node = prop("d:prop/d:displayname")
            display = (name_node.text or "").strip() if name_node is not None else ""
            found.append(DiscoveredCalendar(segment, display or segment, writable, shared, owner))
        found.sort(key=lambda c: c.uri)
        return found

    async def calendar_info(self, z: Credentials, cal: CalendarConfig) -> CalendarInfo:
        body = _bytes(self._propfind_body("d:displayname", "d:sync-token", "d:current-user-privilege-set", "oc:read-only", "c:supported-calendar-component-set"))
        response = await self._request(z, "PROPFIND", self._url(z, cal), body=body, header={"Depth": "0"}, expected=(207,))
        root = await self._multistatus(response)
        name = root.findtext(".//d:displayname", "", NSMAP) or cal.display_name
        token = root.findtext(".//d:sync-token", "", NSMAP)
        permissions = {etree.QName(n).localname for n in root.findall(".//d:current-user-privilege-set/d:privilege/*", NSMAP)}
        read_only = (root.findtext(".//oc:read-only", "", NSMAP) or "").strip() in ("1", "true")
        components = frozenset(n.get("name", "") for n in root.findall(".//c:supported-calendar-component-set/c:comp", NSMAP))
        writable = bool(permissions & {"write", "write-content", "all"}) and not read_only
        return CalendarInfo(name, token, writable, components)

    async def sync(self, z: Credentials, cal: CalendarConfig, token: str | None) -> Delta:
        path = self._path(z, cal)
        changed: dict[str, str] = {}
        deleted: set[str] = set()
        current = token
        for _ in range(MAX_FOLLOW):
            root = _el("d:sync-collection")
            _sub(root, "d:sync-token", current or "")
            _sub(root, "d:sync-level", "1")
            _sub(_sub(root, "d:prop"), "d:getetag")
            response = await self._request(z, "REPORT", self._url(z, cal), body=_bytes(root), header={"Depth": "0"})
            if response.status_code not in (200, 207):
                raise _error(response.status_code, response.content[:2000])
            ms = await self._multistatus(response)
            new = ms.findtext("d:sync-token", "", NSMAP)
            truncated = False
            for r in ms.findall("d:response", NSMAP):
                href = r.findtext("d:href", "", NSMAP)
                status = _status_from(r.findtext("d:status", None, NSMAP))
                if status == 507:
                    truncated = True
                    continue
                name = self._file_from_href(href, path)
                if name is None:
                    continue
                if status == 404:
                    deleted.add(name)
                    changed.pop(name, None)
                    continue
                etag = ""
                for ps in r.findall("d:propstat", NSMAP):
                    if _status_from(ps.findtext("d:status", None, NSMAP)) == 200:
                        etag = normalize_etag(ps.findtext("d:prop/d:getetag", "", NSMAP))
                if etag:
                    changed[name] = etag
                    deleted.discard(name)
            if not new:
                raise CaldavError(CaldavKind.UNEXPECTED, response.status_code)
            if token and (token_number(new) or 0) < (token_number(token) or 0):
                raise CaldavError(CaldavKind.TOKEN_INVALID, response.status_code)
            current = new
            if not truncated:
                return Delta(new, changed, frozenset(deleted), token is None or token == "")
        raise CaldavError(CaldavKind.UNEXPECTED, None)

    def _objects(self, ms: etree._Element, path: str) -> list[DavObject]:
        result = []
        for r in ms.findall("d:response", NSMAP):
            name = self._file_from_href(r.findtext("d:href", "", NSMAP), path)
            if name is None:
                continue
            for ps in r.findall("d:propstat", NSMAP):
                if _status_from(ps.findtext("d:status", None, NSMAP)) != 200:
                    continue
                etag = normalize_etag(ps.findtext("d:prop/d:getetag", "", NSMAP))
                data = ps.findtext("d:prop/c:calendar-data", None, NSMAP)
                if etag and data is not None:
                    result.append(DavObject(name, etag, data))
        return result

    async def get_several(self, z: Credentials, cal: CalendarConfig, hrefs: Sequence[str]) -> list[DavObject]:
        path = self._path(z, cal)
        result: list[DavObject] = []
        for i in range(0, len(hrefs), BLOCK):
            root = _el("c:calendar-multiget")
            prop = _sub(root, "d:prop")
            _sub(prop, "d:getetag")
            _sub(prop, "c:calendar-data")
            for h in hrefs[i : i + BLOCK]:
                _sub(root, "d:href", path + quote(check_filename(h)))
            response = await self._request(z, "REPORT", self._url(z, cal), body=_bytes(root), header={"Depth": "1"}, expected=(207,))
            result.extend(self._objects(await self._multistatus(response), path))
        return result

    async def get(self, z: Credentials, cal: CalendarConfig, href: str) -> DavObject | None:
        response = await self._request(z, "GET", self._url(z, cal, href))
        if response.status_code in (404, 410):
            return None
        if response.status_code != 200:
            raise _error(response.status_code, response.content[:2000])
        etag = normalize_etag(response.headers.get("ETag", ""))
        if not etag:
            raise CaldavError(CaldavKind.UNEXPECTED, 200)
        return DavObject(href, etag, response.text)

    async def _query(self, z: Credentials, cal: CalendarConfig, filter_tree: etree._Element) -> list[DavObject]:
        root = _el("c:calendar-query")
        prop = _sub(root, "d:prop")
        _sub(prop, "d:getetag")
        _sub(prop, "c:calendar-data")
        root.append(filter_tree)
        response = await self._request(z, "REPORT", self._url(z, cal), body=_bytes(root), header={"Depth": "1"}, expected=(207,))
        return self._objects(await self._multistatus(response), self._path(z, cal))

    async def time_range(self, z: Credentials, cal: CalendarConfig, since: datetime, until: datetime) -> list[DavObject]:
        def utc(d: datetime) -> str:
            return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        filt = _el("c:filter")
        vcal = _sub(filt, "c:comp-filter")
        vcal.set("name", "VCALENDAR")
        vev = _sub(vcal, "c:comp-filter")
        vev.set("name", "VEVENT")
        zr = _sub(vev, "c:time-range")
        zr.set("start", utc(since))
        zr.set("end", utc(until))
        return await self._query(z, cal, filt)

    async def search_uid(self, z: Credentials, cal: CalendarConfig, uid: str) -> DavObject | None:
        filt = _el("c:filter")
        vcal = _sub(filt, "c:comp-filter")
        vcal.set("name", "VCALENDAR")
        vev = _sub(vcal, "c:comp-filter")
        vev.set("name", "VEVENT")
        pf = _sub(vev, "c:prop-filter")
        pf.set("name", "UID")
        tm = _sub(pf, "c:text-match", uid)
        tm.set("collation", "i;octet")
        hit = await self._query(z, cal, filt)
        return hit[0] if hit else None

    async def _write(self, z: Credentials, cal: CalendarConfig, href: str, ics: str, header: Mapping[str, str]) -> str | None:
        response = await self._request(z, "PUT", self._url(z, cal, href), body=ics.encode("utf-8"), header=header)
        if response.status_code == 404:
            raise CaldavError(CaldavKind.FORBIDDEN, 404)
        if response.status_code not in (201, 204):
            raise _error(response.status_code, response.content[:2000])
        return normalize_etag(response.headers.get("ETag", "")) or None

    async def create(self, z: Credentials, cal: CalendarConfig, href: str, ics: str) -> str | None:
        return await self._write(z, cal, href, ics, {"If-None-Match": "*"})

    async def change(self, z: Credentials, cal: CalendarConfig, href: str, ics: str, if_match: str) -> str | None:
        return await self._write(z, cal, href, ics, {"If-Match": if_match})

    async def delete(self, z: Credentials, cal: CalendarConfig, href: str, if_match: str) -> bool:
        # For DELETE + If-Match on a missing object the server answers 412; the caller then checks with get() whether it is already gone.
        response = await self._request(z, "DELETE", self._url(z, cal, href), header={"If-Match": if_match})
        if response.status_code in (404, 410):
            return False
        if response.status_code not in (200, 204):
            raise _error(response.status_code, response.content[:2000])
        return True
