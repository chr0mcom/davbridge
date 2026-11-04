#!/usr/bin/env python3
"""Standalone Exchange ActiveSync test client for the EAS facade of DAVbridge (scenarios T1 to T24).

Standard library only. The WBXML codec and the token table are deliberately its OWN (no import from the server package), so that a codec error
of DAVbridge is not hidden by a symmetric error in the client. The password comes as one line from stdin (`--password-stdin`),
never as an argument, and is never printed. Output: structure, status values, HTTP codes, timestamps, counts and its own test markers (the subject starts with "DAVBRIDGE-TEST-").
Merely importing does nothing (no network, no reading of stdin).
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import random
import re
import shlex
import ssl
import struct
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

VERSION = "14.1"
DEVICE_TYPE = "TestClient"
CTYPE = "application/vnd.ms-sync.wbxml"
EAS_PATH = "/Microsoft-Server-ActiveSync"
MARKER = "DAVBRIDGE-TEST-"
AGENT = "eas-testclient/1.0"
PROJECT = Path(__file__).resolve().parent.parent
COMMANDS_ADVERTISED = frozenset(
    {
        "Provision",
        "FolderSync",
        "Settings",
        "Sync",
        "GetItemEstimate",
        "Ping",
        "ItemOperations",
        "SendMail",
        "SmartReply",
        "SmartForward",
        "MeetingResponse",
        "MoveItems",
        "FolderCreate",
        "FolderDelete",
        "FolderUpdate",
        "Search",
        "ResolveRecipients",
    }
)
COMMAND_CODES = {
    "Sync": 0,
    "SendMail": 1,
    "SmartForward": 2,
    "SmartReply": 3,
    "GetAttachment": 4,
    "FolderSync": 9,
    "FolderCreate": 10,
    "FolderDelete": 11,
    "FolderUpdate": 12,
    "MoveItems": 13,
    "GetItemEstimate": 14,
    "MeetingResponse": 15,
    "Search": 16,
    "Settings": 17,
    "Ping": 18,
    "ItemOperations": 19,
    "Provision": 20,
    "ResolveRecipients": 21,
    "ValidateCert": 22,
    "Find": 23,
}


# ---------------------------------------------------------------- WBXML (own codec, own token table)

# page: (page name, "Tag=Token ..." in hex). Only the tags the client needs; checked against tests/data/eas_tags.txt.
_TABLE = {
    0: (
        "AirSync",
        "Sync=05 Responses=06 Add=07 Change=08 Delete=09 Fetch=0A SyncKey=0B ClientId=0C ServerId=0D Status=0E Collection=0F Class=10 "
        "CollectionId=12 GetChanges=13 MoreAvailable=14 WindowSize=15 Commands=16 Options=17 FilterType=18 Conflict=1B Collections=1C "
        "ApplicationData=1D DeletesAsMoves=1E MIMESupport=22 Wait=24 Limit=25 HeartbeatInterval=29",
    ),
    4: (
        "Calendar",
        "Timezone=05 AllDayEvent=06 Attendees=07 Attendee=08 BusyStatus=0D Categories=0E Category=0F DtStamp=11 EndTime=12 Exception=13 "
        "Exceptions=14 Deleted=15 ExceptionStartTime=16 Location=17 MeetingStatus=18 OrganizerEmail=19 OrganizerName=1A Recurrence=1B "
        "Type=1C Until=1D Occurrences=1E Interval=1F DayOfWeek=20 DayOfMonth=21 WeekOfMonth=22 MonthOfYear=23 Reminder=24 "
        "Sensitivity=25 Subject=26 StartTime=27 UID=28",
    ),
    5: ("Move", "MoveItems=05 Move=06 SrcMsgId=07 SrcFldId=08 DstFldId=09 Response=0A Status=0B DstMsgId=0C"),
    6: ("GetItemEstimate", "GetItemEstimate=05 Collections=07 Collection=08 Class=09 CollectionId=0A Estimate=0C Response=0D Status=0E"),
    7: (
        "FolderHierarchy",
        "Folders=05 Folder=06 DisplayName=07 ServerId=08 ParentId=09 Type=0A Status=0C Changes=0E Add=0F Delete=10 Update=11 "
        "SyncKey=12 FolderCreate=13 FolderDelete=14 FolderUpdate=15 FolderSync=16 Count=17",
    ),
    8: ("MeetingResponse", "CalendarId=05 CollectionId=06 MeetingResponse=07 RequestId=08 Request=09 Result=0A Status=0B UserResponse=0C"),
    10: ("ResolveRecipients", "ResolveRecipients=05 Response=06 Status=07 Type=08 Recipient=09 DisplayName=0A EmailAddress=0B Options=0F To=10"),
    13: ("Ping", "Ping=05 Status=07 HeartbeatInterval=08 Folders=09 Folder=0A Id=0B Class=0C MaxFolders=0D"),
    14: ("Provision", "Provision=05 Policies=06 Policy=07 PolicyType=08 PolicyKey=09 Data=0A Status=0B RemoteWipe=0C EASProvisionDoc=0D DevicePasswordEnabled=0E"),
    15: ("Search", "Search=05 Store=07 Name=08 Query=09 Options=0A Range=0B Status=0C Response=0D Result=0E Properties=0F Total=10 FreeText=15"),
    17: ("AirSyncBase", "BodyPreference=05 Type=06 TruncationSize=07 Body=0A Data=0B EstimatedDataSize=0C Truncated=0D"),
    18: (
        "Settings",
        "Settings=05 Status=06 Get=07 Set=08 Oof=09 OofState=0A BodyType=13 DeviceInformation=16 Model=17 FriendlyName=19 OS=1A "
        "OSLanguage=1B UserInformation=1D EmailAddresses=1E SMTPAddress=1F UserAgent=20 PrimarySmtpAddress=23 Accounts=24 "
        "Account=25 SendDisabled=29",
    ),
    20: ("ItemOperations", "ItemOperations=05 Fetch=06 Store=07 Options=08 Range=09 Total=0A Properties=0B Data=0C Status=0D Response=0E EmptyFolderContents=12 DeleteSubFolders=13"),
    21: ("ComposeMail", "SendMail=05 SmartForward=06 SmartReply=07 SaveInSentItems=08 Source=0B FolderId=0C ItemId=0D Mime=10 ClientId=11 Status=12"),
}
NAME_TO_TOKEN: dict[str, tuple[int, int]] = {}
TOKEN_TO_NAME: dict[tuple[int, int], str] = {}
for _page, (_sname, _list) in _TABLE.items():
    for _entry in _list.split():
        _n, _t = _entry.split("=")
        NAME_TO_TOKEN[f"{_sname}:{_n}"] = (_page, int(_t, 16))
        TOKEN_TO_NAME[(_page, int(_t, 16))] = f"{_sname}:{_n}"

HEADER = b"\x03\x01\x6a\x00"
SWITCH_PAGE, END, STR_I, OPAQUE = 0x00, 0x01, 0x03, 0xC3


class WbxmlError(Exception):
    pass


class Node:
    """A node: page, token, text (STR_I), data (OPAQUE) and children."""

    __slots__ = ("page", "token", "text", "data", "children")

    def __init__(self, page: int, token: int, text: str | None = None, data: bytes | None = None, children: tuple = ()):
        self.page, self.token, self.text, self.data, self.children = page, token, text, data, tuple(children)

    def __eq__(self, other):
        return isinstance(other, Node) and (self.page, self.token, self.text or None, self.data, self.children) == (other.page, other.token, other.text or None, other.data, other.children)

    def __hash__(self):
        return hash((self.page, self.token, self.text, self.data, self.children))

    def __repr__(self):
        return f"Kn({display(self)!r}, text={self.text!r}, daten={self.data!r}, kinder={list(self.children)!r})"


def K(qname: str, *children, text=None, data: bytes | None = None) -> Node:
    """Builds a node from 'Page:Name'; None children are skipped; numbers become text."""
    page, token = NAME_TO_TOKEN[qname]
    return Node(page, token, None if text is None else str(text), data, tuple(k for k in children if k is not None))


def display(k: Node) -> str:
    return TOKEN_TO_NAME.get((k.page, k.token)) or f"{k.page}:0x{k.token:02X}"


def _mb(n: int) -> bytes:
    parts = [n & 0x7F]
    n >>= 7
    while n:
        parts.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(parts))


def _mb_read(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for _ in range(5):
        if pos >= len(data):
            raise WbxmlError("truncated")
        b = data[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, pos
    raise WbxmlError("mb_u_int32 zu lang")


def encode(root: Node) -> bytes:
    out = bytearray(HEADER)
    page = 0
    work: list = [root]
    while work:
        x = work.pop()
        if x is END:
            out.append(END)
            continue
        if x.page != page:
            out += bytes((SWITCH_PAGE, x.page))
            page = x.page
        if not 0x05 <= x.token <= 0x3F:
            raise ValueError("token outside 0x05..0x3F")
        content = bool(x.children) or bool(x.text) or x.data is not None
        out.append(x.token | (0x40 if content else 0))
        if content:
            if x.text:
                if "\x00" in x.text:
                    raise ValueError("NUL in text")
                out += bytes((STR_I,)) + x.text.encode("utf-8") + b"\x00"
            if x.data is not None:
                out += bytes((OPAQUE,)) + _mb(len(x.data)) + x.data
            work.append(END)
            work.extend(reversed(x.children))
    return bytes(out)


def decode(data: bytes) -> Node:
    """Iterative decoder. Attributes, literals, string table references and entities are not supported (WbxmlError)."""
    if len(data) < 4:
        raise WbxmlError("zu kurz")
    pos = 1
    identifier, pos = _mb_read(data, pos)
    if identifier == 0:
        _, pos = _mb_read(data, pos)
    _charset, pos = _mb_read(data, pos)
    tab, pos = _mb_read(data, pos)
    pos += tab
    page = 0
    stack: list[list] = []
    root = None
    n = len(data)
    while pos < n:
        if root is not None:
            raise WbxmlError("data after the root element")
        b = data[pos]
        pos += 1
        if b == SWITCH_PAGE:
            if pos >= n:
                raise WbxmlError("truncated")
            page = data[pos]
            pos += 1
        elif b == END:
            if not stack:
                raise WbxmlError("END without element")
            s, t, text, dat, children = stack.pop()
            k = Node(s, t, text, dat, tuple(children))
            if stack:
                stack[-1][4].append(k)
            else:
                root = k
        elif b == STR_I:
            end = data.find(b"\x00", pos)
            if end < 0 or not stack:
                raise WbxmlError("STR_I")
            try:
                s = data[pos:end].decode("utf-8")
            except UnicodeDecodeError as e:
                raise WbxmlError("UTF-8") from e
            stack[-1][2] = (stack[-1][2] or "") + s
            pos = end + 1
        elif b == OPAQUE:
            length, pos = _mb_read(data, pos)
            if pos + length > n or not stack:
                raise WbxmlError("OPAQUE")
            stack[-1][3] = (stack[-1][3] or b"") + data[pos : pos + length]
            pos += length
        else:
            if b & 0x80 or (b & 0x3F) < 0x05:
                raise WbxmlError(f"unsupported token 0x{b:02X}")
            if b & 0x40:
                stack.append([page, b & 0x3F, None, None, []])
            else:
                k = Node(page, b & 0x3F)
                if stack:
                    stack[-1][4].append(k)
                else:
                    root = k
    if stack or root is None:
        raise WbxmlError("truncated")
    return root


def kind(k: Node | None, qname: str) -> Node | None:
    """First direct child."""
    if k is None:
        return None
    t = NAME_TO_TOKEN[qname]
    for c in k.children:
        if (c.page, c.token) == t:
            return c
    return None


def children(k: Node | None, qname: str) -> list[Node]:
    if k is None:
        return []
    t = NAME_TO_TOKEN[qname]
    return [c for c in k.children if (c.page, c.token) == t]


def path(k: Node | None, p: str) -> Node | None:
    """Pfad aus direkten Kindern, 'A:x/B:y/C:z'."""
    for part in p.split("/"):
        k = kind(k, part)
        if k is None:
            return None
    return k


def txt(k: Node | None, p: str) -> str | None:
    n = path(k, p)
    return None if n is None else (n.text or "")


def search(k: Node | None, qname: str) -> list[Node]:
    """All descendants with this name (depth-first, document order)."""
    if k is None:
        return []
    t = NAME_TO_TOKEN[qname]
    result, stack = [], list(reversed(k.children))
    while stack:
        c = stack.pop()
        if (c.page, c.token) == t:
            result.append(c)
        stack.extend(reversed(c.children))
    return result


def number(s: str | None) -> int | None:
    try:
        return int(s) if s is not None else None
    except ValueError:
        return None


VALUES_ALLOWED = frozenset(
    {
        "Status",
        "MoreAvailable",
        "WindowSize",
        "Count",
        "Class",
        "CollectionId",
        "Type",
        "HeartbeatInterval",
        "MaxFolders",
        "Total",
        "AllDayEvent",
        "BusyStatus",
        "Sensitivity",
        "Interval",
        "DayOfWeek",
        "DayOfMonth",
        "WeekOfMonth",
        "MonthOfYear",
        "Occurrences",
        "Deleted",
        "OofState",
        "SendDisabled",
        "PolicyType",
        "Estimate",
        "FilterType",
        "Conflict",
        "GetChanges",
        "DeletesAsMoves",
        "Wait",
        "Limit",
        "MeetingStatus",
        "Reminder",
        "Truncated",
        "DevicePasswordEnabled",
        "Id",
        "ParentId",
        "RecipientCount",
    }
)


def skeleton(k: Node, indentation: int = 0) -> list[str]:
    """Structure lines without content: text only for enumerations/numbers and own test markers, otherwise only the length."""
    name = display(k)
    short = name.split(":")[-1]
    extra = ""
    if k.text is not None:
        if short in VALUES_ALLOWED or (short == "Subject" and k.text.startswith(MARKER)) or k.text.startswith(MARKER):
            extra = f" = {k.text}"
        else:
            extra = f" = <{len(k.text)} Zeichen>"
    if k.data is not None:
        extra += f" <{len(k.data)} Byte>"
    lines = ["  " * indentation + name + extra]
    for c in k.children:
        lines += skeleton(c, indentation + 1)
    return lines


def signature(k: Node):
    """Complete comparison value (for comparing only, never printed)."""
    return (k.page, k.token, k.text or None, k.data, tuple(signature(c) for c in k.children))


# ---------------------------------------------------------------- helpers: time, time zone, appointment


def now() -> datetime:
    return datetime.now(timezone.utc)


def moment() -> str:
    return now().strftime("%H:%M:%SZ")


def compact(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def tz_blob_berlin() -> bytes:
    """TIME_ZONE_INFORMATION (172 bytes) for Europe/Berlin (W. Europe Standard Time)."""

    def name(s: str) -> bytes:
        return s.encode("utf-16-le").ljust(64, b"\x00")[:64]

    default = struct.pack("<8H", 0, 10, 0, 5, 3, 0, 0, 0)
    summer = struct.pack("<8H", 0, 3, 0, 5, 2, 0, 0, 0)
    blob = struct.pack("<l", -60) + name("W. Europe Standard Time") + default + struct.pack("<l", 0) + name("W. Europe Daylight Time") + summer + struct.pack("<l", -60)
    assert len(blob) == 172
    return blob


TZ_BASE64 = base64.b64encode(tz_blob_berlin()).decode("ascii")


def new_marker(extra: str = "") -> str:
    return f"{MARKER}{time.strftime('%m%d-%H%M%S')}-{random.randrange(16**3):03x}{extra}"


def appointment(
    marker: str,
    *,
    days: int = 1,
    all_day: bool = False,
    location: str | None = "DAVBRIDGE-TEST-Location",
    reminder: int | None = 15,
    sens: int = 0,
    uid: str | None = None,
    series: bool = False,
    note: str = "DAVBRIDGE-TEST-Note",
) -> Node:
    """ApplicationData of a test appointment (starts tomorrow 09:00 UTC, one hour; all-day: midnight Europe/Berlin in UTC)."""
    if all_day:
        try:
            from zoneinfo import ZoneInfo

            tag = (now() + timedelta(days=days)).date()
            berlin = ZoneInfo("Europe/Berlin")
            next_day = tag + timedelta(days=1)
            start = datetime(tag.year, tag.month, tag.day, tzinfo=berlin).astimezone(timezone.utc)
            end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=berlin).astimezone(timezone.utc)
        except Exception as e:
            raise Error("All-day test appointments need zoneinfo with Europe/Berlin") from e
    else:
        start = (now() + timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
    return K(
        "AirSync:ApplicationData",
        K("Calendar:Timezone", text=TZ_BASE64),
        K("Calendar:AllDayEvent", text="1" if all_day else "0"),
        K("Calendar:BusyStatus", text="2"),
        K("Calendar:DtStamp", text=compact(now())),
        K("Calendar:EndTime", text=compact(end)),
        K("Calendar:Location", text=location) if location else None,
        K("Calendar:Recurrence", K("Calendar:Type", text="0"), K("Calendar:Interval", text="1"), K("Calendar:Occurrences", text="3")) if series else None,
        K("Calendar:Reminder", text=reminder) if reminder is not None else None,
        K("Calendar:Sensitivity", text=sens),
        K("Calendar:Subject", text=marker),
        K("Calendar:StartTime", text=compact(start)),
        K("Calendar:UID", text=uid) if uid else None,
        K("AirSyncBase:Body", K("AirSyncBase:Type", text="1"), K("AirSyncBase:Data", text=note)),
    )


# ---------------------------------------------------------------- HTTP


class Error(Exception):
    """Abort with a message (exit code 2), for example a missing password or a folder that was not found."""


class PasswordError(Error):
    """No password on stdin: aborts the whole run (exit code 2), does not count as a scenario failure."""


class Reply:
    def __init__(self, status: int, header: list[tuple[str, str]], payload: bytes, duration: float):
        self.status, self.header, self.payload, self.duration = status, header, payload, duration
        self.root: Node | None = None
        self.wbxml_error: str | None = None
        if status == 200 and payload and ("wbxml" in (self.h("content-type") or "").lower() or payload[:1] in (b"\x02", b"\x03")):
            try:
                self.root = decode(payload)
            except WbxmlError as e:
                self.wbxml_error = str(e)

    def h(self, name: str) -> str | None:
        values = self.all(name)
        return ", ".join(values) if values else None

    def all(self, name: str) -> list[str]:
        return [v for k, v in self.header if k.lower() == name.lower()]

    def json(self):
        try:
            return json.loads(self.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None


class Http:
    """Thin layer over http.client; one connection per request. Replaceable in tests (same method `request`)."""

    def __init__(self, base: str, tls_check: bool = True, timeout: float = 60.0):
        t = urlsplit(base)
        if t.scheme not in ("http", "https") or not t.hostname:
            raise Error("--url must be http(s)://host[:port]")
        self.schema, self.host, self.port, self.timeout = t.scheme, t.hostname, t.port, timeout
        self.base = f"{t.scheme}://{t.netloc}"
        self.ctx = None
        if t.scheme == "https":
            self.ctx = ssl.create_default_context()
            if not tls_check:
                self.ctx.check_hostname = False
                self.ctx.verify_mode = ssl.CERT_NONE

    def request(self, method: str, p: str, header: dict, payload: bytes | None = None, timeout: float | None = None) -> Reply:
        t0 = time.monotonic()
        tmo = timeout or self.timeout
        if self.schema == "https":
            c = http.client.HTTPSConnection(self.host, self.port, timeout=tmo, context=self.ctx)
        else:
            c = http.client.HTTPConnection(self.host, self.port, timeout=tmo)
        try:
            try:
                c.request(method, p, body=payload, headers=header)
            except (BrokenPipeError, ConnectionResetError):
                pass
            r = c.getresponse()
            data = r.read()
            return Reply(r.status, list(r.getheaders()), data, time.monotonic() - t0)
        except (OSError, http.client.HTTPException) as e:
            raise Error(f"Network error at {method} {p.split('?')[0]}: {type(e).__name__}") from e
        finally:
            c.close()


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------- state (JSON, 0600)


class State:
    def __init__(self, file: Path):
        self.file = file
        self.lock = threading.Lock()
        self.data: dict = {"devices": {}}
        try:
            self.data = json.loads(file.read_text(encoding="utf-8"))
            self.data.setdefault("devices", {})
        except (OSError, ValueError):
            pass

    def g(self, device: str) -> dict:
        d = self.data["devices"].setdefault(device, {})
        d.setdefault("folders", {})
        d.setdefault("sync", {})
        d.setdefault("tests", {})
        return d

    def store(self) -> None:
        with self.lock:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.file.parent, 0o700)
            except OSError:
                pass
            tmp = self.file.with_name(self.file.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.file)
            os.chmod(self.file, 0o600)

    def delete(self) -> None:
        try:
            self.file.unlink()
        except OSError:
            pass
        self.data = {"devices": {}}


class Context:
    """Shared settings of a run: user, base URL, device id, password (lazily from stdin), state, output."""

    def __init__(self, args, http_obj: Http | None = None, stdin=None):
        self.args = args
        self.user = args.user
        self.device = args.device
        self.http = http_obj or Http(args.url, tls_check=not getattr(args, "tls_insecure", False), timeout=getattr(args, "timeout", 60.0))
        self.base = self.http.base
        self.host = self.http.host
        self.state = State(Path(args.state).expanduser() if args.state else Path.home() / ".cache" / "eas-testclient" / f"{self.user}.json")
        self._pw: str | None = None
        self._stdin = stdin
        self.pressure = threading.Lock()
        self.quiet = getattr(args, "quiet", False)
        self.verbose = getattr(args, "verbose", False)

    def password(self) -> str:
        if self._pw is None:
            s = self._stdin or sys.stdin
            if hasattr(s, "isatty") and s.isatty():
                raise PasswordError("The password is expected as one line on stdin (--password-stdin)")
            line = s.readline()
            if not line:
                raise PasswordError("No password on stdin")
            self._pw = line.rstrip("\r\n")
        return self._pw

    def p(self, text: str = "") -> None:
        with self.pressure:
            print(text, flush=True)

    def log(self, text: str) -> None:
        if not self.quiet:
            self.p(f"    {text}")


def status_short(a: Reply) -> str:
    if a.root is None:
        return ""
    for k in _all_node(a.root):
        if display(k).endswith(":Status"):
            return f"Status={k.text}"
    return ""


def _all_node(k: Node):
    stack = list(reversed(k.children))
    while stack:
        c = stack.pop()
        yield c
        stack.extend(reversed(c.children))


# ---------------------------------------------------------------- results


class Run:
    """Collects the checks of a scenario and prints them."""

    def __init__(self, ctx: Context, identifier: str, title: str):
        self.ctx, self.identifier, self.title = ctx, identifier, title
        self.ok_n = self.fail_n = self.skip_n = 0

    def log(self, text: str) -> None:
        self.ctx.log(text)

    def header(self) -> None:
        self.ctx.p(f"== {self.identifier} {self.title}  ({moment()})")

    def check(self, text: str, condition, detail: str = "") -> bool:
        ok = bool(condition)
        if ok:
            self.ok_n += 1
        else:
            self.fail_n += 1
        self.ctx.p(f"  [{'ok' if ok else 'ERROR'}] {text}" + (f"  ({detail})" if detail else ""))
        return ok

    def hint(self, text: str) -> None:
        self.ctx.p(f"  [hint] {text}")

    def skipped(self, text: str) -> None:
        self.skip_n += 1
        self.ctx.p(f"  [skipped] {text}")

    @property
    def verdict(self) -> str:
        if self.fail_n:
            return "FAIL"
        if self.skip_n and not self.ok_n:
            return "SKIP"
        return "PASS"


# ---------------------------------------------------------------- device: EAS requests


class SyncResult:
    def __init__(self, a: Reply):
        self.a = a
        w = a.root
        self.empty = not a.payload
        self.global_status = 1 if self.empty else number(txt(w, "AirSync:Status"))
        col = path(w, "AirSync:Collections/AirSync:Collection")
        self.col = col
        self.status = number(txt(col, "AirSync:Status")) if col is not None else self.global_status  # empty body = nothing to do
        self.key = txt(col, "AirSync:SyncKey") if col is not None else None
        self.more = col is not None and kind(col, "AirSync:MoreAvailable") is not None
        cmds = kind(col, "AirSync:Commands")
        self.adds = [(txt(c, "AirSync:ServerId"), kind(c, "AirSync:ApplicationData")) for c in children(cmds, "AirSync:Add")]
        self.changes = [(txt(c, "AirSync:ServerId"), kind(c, "AirSync:ApplicationData")) for c in children(cmds, "AirSync:Change")]
        self.deletes = [txt(c, "AirSync:ServerId") for c in children(cmds, "AirSync:Delete")]
        self.has_commands = cmds is not None
        resp = kind(col, "AirSync:Responses")
        self.r_add = [(txt(c, "AirSync:ClientId"), txt(c, "AirSync:ServerId"), number(txt(c, "AirSync:Status"))) for c in children(resp, "AirSync:Add")]
        self.r_change = [(txt(c, "AirSync:ServerId"), number(txt(c, "AirSync:Status"))) for c in children(resp, "AirSync:Change")]
        self.r_delete = [(txt(c, "AirSync:ServerId"), number(txt(c, "AirSync:Status"))) for c in children(resp, "AirSync:Delete")]

    @property
    def count(self) -> int:
        return len(self.adds) + len(self.changes) + len(self.deletes)


def element_info(ad: Node | None) -> dict:
    subj = txt(ad, "Calendar:Subject")
    exc = path(ad, "Calendar:Exceptions")
    return {
        "marker": subj if subj and subj.startswith(MARKER) else None,
        "series": path(ad, "Calendar:Recurrence") is not None,
        "exceptions": len(children(exc, "Calendar:Exception")),
        "private": txt(ad, "Calendar:Sensitivity") == "2",
        "all_day": txt(ad, "Calendar:AllDayEvent") == "1",
    }


class Full:
    def __init__(self):
        self.elements: dict[str, dict] = {}
        self.requests = 0
        self.window: list[int] = []
        self.bytes_max = 0
        self.duration = 0.0
        self.double = 0
        self.changes = self.deletes = 0
        self.key: str | None = None
        self.error: list[str] = []

    @property
    def series(self) -> int:
        return sum(1 for e in self.elements.values() if e["series"])

    @property
    def private(self) -> int:
        return sum(1 for e in self.elements.values() if e["private"])

    @property
    def with_marker(self) -> dict[str, dict]:
        return {s: e for s, e in self.elements.items() if e["marker"]}


class Device:
    """A (simulated) device: its own DeviceId and its own SyncKey chains in the state."""

    def __init__(self, ctx: Context, device_id: str):
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", device_id):
            raise Error("--device must be [A-Za-z0-9]{1,32} (derived IDs get a short suffix)")
        self.ctx, self.id = ctx, device_id
        self.policy_key: str | None = None

    @property
    def st(self) -> dict:
        return self.ctx.state.g(self.id)

    def derived(self, suffix: str) -> "Device":
        return Device(self.ctx, (self.id[: 32 - len(suffix)] + suffix))

    # ---- Transport
    def eas(
        self,
        cmd: str,
        root: Node | None = None,
        *,
        raw: bytes | None = None,
        version: str = VERSION,
        ctype: str = CTYPE,
        auth: bool = True,
        password: str | None = None,
        timeout: float | None = None,
        method: str = "POST",
        silent: bool = False,
    ) -> Reply:
        c = self.ctx
        payload = raw if raw is not None else (encode(root) if root is not None else b"")
        p = f"{EAS_PATH}?Cmd={quote(cmd, safe='')}&User={quote(c.user, safe='')}&DeviceId={self.id}&DeviceType={DEVICE_TYPE}"
        header = {"MS-ASProtocolVersion": version, "Content-Type": ctype, "User-Agent": AGENT}
        if auth:
            header["Authorization"] = basic(c.user, password if password is not None else c.password())
        if self.policy_key:
            header["X-MS-PolicyKey"] = self.policy_key
        a = c.http.request(method, p, header, payload, timeout)
        if not silent:
            c.log(f"{moment()}  {cmd:<16} HTTP {a.status}  {int(a.duration * 1000)} ms  {len(payload)}->{len(a.payload)} B  {status_short(a)}")
        if c.verbose and a.root is not None and not silent:
            for z in skeleton(a.root):
                c.log("      " + z)
        return a

    def options(self, *, auth: bool = True, password: str | None = None) -> Reply:
        c = self.ctx
        header = {"User-Agent": AGENT}
        if auth:
            header["Authorization"] = basic(c.user, password if password is not None else c.password())
        a = c.http.request("OPTIONS", EAS_PATH, header)
        c.log(f"{moment()}  OPTIONS          HTTP {a.status}  {int(a.duration * 1000)} ms")
        return a

    # ---- FolderSync
    def foldersync(self, key: str | None = None) -> tuple[Reply, list[dict]]:
        key = key if key is not None else self.st.get("hier_key", "0")
        a = self.eas("FolderSync", K("FolderHierarchy:FolderSync", K("FolderHierarchy:SyncKey", text=key)))
        w = a.root
        folder = []
        for f in search(kind(w, "FolderHierarchy:Changes"), "FolderHierarchy:Add"):
            folder.append(
                {"id": txt(f, "FolderHierarchy:ServerId"), "name": txt(f, "FolderHierarchy:DisplayName"), "type": number(txt(f, "FolderHierarchy:Type")), "parent": txt(f, "FolderHierarchy:ParentId")}
            )
        if a.status == 200 and number(txt(w, "FolderHierarchy:Status")) == 1:
            self.st["hier_key"] = txt(w, "FolderHierarchy:SyncKey") or key
            if key == "0" or folder:
                self.st["folders"] = {o["id"]: o for o in folder}
            self.ctx.state.store()
        return a, folder

    def folder_list(self) -> dict[str, dict]:
        if not self.st["folders"]:
            self.foldersync("0")
        if not self.st["folders"]:
            raise Error("FolderSync returned no folders")
        return self.st["folders"]

    def calendar(self, pattern: str) -> dict:
        """Calendar folder (type 8/13) by folder id or display name (substring, case-insensitive)."""
        listing = self.folder_list()
        cals = [o for o in listing.values() if o["type"] in (8, 13)]
        for o in cals:
            if o["id"] == pattern:
                return o
        m = pattern.casefold()
        for o in cals:
            if (o["name"] or "").casefold() == m:
                return o
        for o in cals:
            if m in (o["name"] or "").casefold():
                return o
        raise Error(f"Calendar '{pattern}' not found; available: {', '.join(repr(o['name']) for o in cals) or '-'}")

    # ---- Sync
    @staticmethod
    def sync_root(key: str, cid: str, *, window: int | None = None, commands: list[Node] | None = None, get_changes: bool | None = None, options: bool = True) -> Node:
        is_null = key == "0"
        opt = None
        if options and not is_null:
            opt = K(
                "AirSync:Options",
                K("AirSync:FilterType", text="0"),
                K("AirSync:Conflict", text="1"),
                K("AirSyncBase:BodyPreference", K("AirSyncBase:Type", text="1"), K("AirSyncBase:TruncationSize", text="32768")),
            )
        col = K(
            "AirSync:Collection",
            K("AirSync:SyncKey", text=key),
            K("AirSync:CollectionId", text=cid),
            K("AirSync:GetChanges", text="1" if get_changes else "0") if get_changes is not None and not is_null else None,
            K("AirSync:WindowSize", text=window) if window and not is_null else None,
            opt,
            K("AirSync:Commands", *commands) if commands else None,
        )
        return K("AirSync:Sync", K("AirSync:Collections", col))

    def sync(self, cid: str, key: str, **kw) -> SyncResult:
        silent = kw.pop("silent", False)
        a = self.eas("Sync", self.sync_root(key, cid, **kw), silent=silent)
        if a.status != 200:
            raise Error(f"Sync returned HTTP {a.status}")
        if a.wbxml_error:
            raise Error(f"Sync response not decodable ({a.wbxml_error})")
        return SyncResult(a)

    def sync_full(self, cid: str, window: int = 100, silent: bool = False) -> Full:
        v = Full()
        t0 = time.monotonic()
        r = self.sync(cid, "0", silent=silent)
        v.requests += 1
        v.bytes_max = max(v.bytes_max, len(r.a.payload))
        if r.status != 1 or not r.key:
            raise Error(f"Sync 0 for {cid}: Status {r.status}")
        key = r.key
        for _ in range(5000):
            r = self.sync(cid, key, window=window, get_changes=True, silent=silent)
            v.requests += 1
            v.bytes_max = max(v.bytes_max, len(r.a.payload))
            if r.status != 1:
                v.error.append(f"Sync status {r.status}")
                break
            for sid, ad in r.adds + r.changes:
                if sid in v.elements:
                    v.double += 1
                v.elements[sid] = element_info(ad)
            for sid in r.deletes:
                v.elements.pop(sid, None)
            v.changes += len(r.changes)
            v.deletes += len(r.deletes)
            v.window.append(r.count)
            if r.key:
                key = r.key
            if not r.more:
                break
            if r.count == 0:
                v.error.append("MoreAvailable without items")
                break
        else:
            v.error.append("Abort: too many windows")
        v.key = key
        v.duration = time.monotonic() - t0
        self.st["sync"][cid] = key
        self.ctx.state.store()
        return v

    def key(self, cid: str) -> str:
        k = self.st["sync"].get(cid)
        if not k:
            self.sync_full(cid, silent=True)
            k = self.st["sync"][cid]
        return k

    def sync_commands(self, cid: str, commands: list[Node], **kw) -> SyncResult:
        key = self.key(cid)
        r = self.sync(cid, key, commands=commands, **kw)
        if r.status == 3:
            self.sync_full(cid, silent=True)
            r = self.sync(cid, self.st["sync"][cid], commands=commands, **kw)
        if r.key:
            self.st["sync"][cid] = r.key
            self.ctx.state.store()
        return r

    # ---- test appointments
    def remember_test(self, cid: str, sid: str, marker: str, client: str | None = None) -> None:
        listing = self.st["tests"].setdefault(cid, [])
        listing[:] = [t for t in listing if t["sid"] != sid]
        listing.append({"sid": sid, "marker": marker, "client": client})
        self.ctx.state.store()

    def forget_test(self, cid: str, sid: str) -> None:
        listing = self.st["tests"].setdefault(cid, [])
        listing[:] = [t for t in listing if t["sid"] != sid]
        self.ctx.state.store()

    def last_test(self, cid: str) -> dict | None:
        listing = self.st["tests"].get(cid) or []
        return listing[-1] if listing else None

    def add_appointment(self, cid: str, marker: str | None = None, **kw) -> tuple[SyncResult, str, str, str | None]:
        """Add via Sync; returns (result, ClientId, marker, ServerId|None). Remembers the appointment on success."""
        marker = marker or new_marker()
        client = uuid.uuid4().hex[:16]
        r = self.sync_commands(cid, [K("AirSync:Add", K("AirSync:ClientId", text=client), appointment(marker, uid=uuid.uuid4().hex.upper(), **kw))])
        sid = next((s for c, s, st in r.r_add if c == client and st == 1 and s), None)
        if sid:
            self.remember_test(cid, sid, marker, client)
        return r, client, marker, sid

    def change_appointment(self, cid: str, sid: str, marker: str, **kw) -> SyncResult:
        return self.sync_commands(cid, [K("AirSync:Change", K("AirSync:ServerId", text=sid), appointment(marker, **kw))])

    def delete_appointment(self, cid: str, sid: str) -> SyncResult:
        r = self.sync_commands(cid, [K("AirSync:Delete", K("AirSync:ServerId", text=sid))])
        self.forget_test(cid, sid)
        return r

    # ---- Ping
    def ping(self, cids: list[str], heartbeat: int, timeout: float | None = None, silent: bool = False) -> tuple[Reply, dict]:
        w = K("Ping:Ping", K("Ping:HeartbeatInterval", text=heartbeat), K("Ping:Folders", *[K("Ping:Folder", K("Ping:Id", text=c), K("Ping:Class", text="Calendar")) for c in cids]))
        a = self.eas("Ping", w, timeout=timeout or (heartbeat + 40), silent=silent)
        r = a.root
        return a, {
            "status": number(txt(r, "Ping:Status")),
            "hb": number(txt(r, "Ping:HeartbeatInterval")),
            "ids": [f.text for f in search(kind(r, "Ping:Folders"), "Ping:Folder")],
            "max": number(txt(r, "Ping:MaxFolders")),
        }


# ---------------------------------------------------------------- scenarios


def p_status(a: Reply, p: str) -> int | None:
    return number(txt(a.root, p))


def t1_options(g: Device, l: Run, args) -> None:
    a = g.options(auth=False)
    l.check("OPTIONS without login -> 401", a.status == 401, f"HTTP {a.status}")
    schemas = [w.split()[0].lower() for w in a.all("www-authenticate") if w.strip()]
    l.check("WWW-Authenticate offers only Basic (never Bearer/Negotiate)", schemas and all(s == "basic" for s in schemas), f"Schemata: {','.join(schemas) or '-'}")
    a = g.options()
    if not l.check("OPTIONS with login -> 200", a.status == 200, f"HTTP {a.status}"):
        return
    versions = [v.strip() for v in (a.h("ms-asprotocolversions") or "").split(",") if v.strip()]
    l.check("MS-ASProtocolVersions is exactly 14.1", versions == ["14.1"], f"Versionen: {','.join(versions) or '-'}")
    commands = [v.strip() for v in (a.h("ms-asprotocolcommands") or "").split(",") if v.strip()]
    l.check(
        "MS-ASProtocolCommands contains the 17 advertised commands",
        set(commands) == COMMANDS_ADVERTISED and len(commands) == 17,
        f"{len(commands)} commands; missing: {','.join(sorted(COMMANDS_ADVERTISED - set(commands))) or '-'}; additional: {','.join(sorted(set(commands) - COMMANDS_ADVERTISED)) or '-'}",
    )
    l.check("MS-Server-ActiveSync is 14.1", (a.h("ms-server-activesync") or "").strip() == "14.1", f"Value: {a.h('ms-server-activesync') or '-'}")
    l.check("no Bearer/Negotiate in the 200 response", not any(w.lower().startswith(("bearer", "negotiate")) for w in a.all("www-authenticate")))
    l.hint(f"Allow: {a.h('allow') or '-'} · Public: {a.h('public') or '-'} (expected OPTIONS,POST)")
    a = g.options(password="wrong-" + uuid.uuid4().hex[:8])
    l.check("OPTIONS with a wrong password -> 401", a.status == 401, f"HTTP {a.status}")


POX_REQUEST = (
    '<?xml version="1.0" encoding="utf-8"?><Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/mobilesync/requestschema/2006">'
    "<Request><EMailAddress>{mail}</EMailAddress><AcceptableResponseSchema>http://schemas.microsoft.com/exchange/autodiscover/mobilesync/responseschema/2006"
    "</AcceptableResponseSchema></Request></Autodiscover>"
)


def t2_autodiscover(g: Device, l: Run, args) -> None:
    c = g.ctx
    http_ = c.http
    mail = f"{c.user}@{c.host}"
    other = f"random{uuid.uuid4().hex[:6]}@{c.host}"
    foreign = f"{c.user}@foreign-domain.invalid"

    def get(p: str) -> Reply:
        a = http_.request("GET", p, {"User-Agent": AGENT})
        c.log(f"{moment()}  GET {p.split('?')[0][:48]:<48} HTTP {a.status}")
        return a

    q = lambda m, proto: f"/autodiscover/autodiscover.json?Email={quote(m)}&Protocol={proto}"  # noqa: E731
    a = get(q(mail, "ActiveSync"))
    j = a.json()
    l.check("JSON ActiveSync anonymous -> 200", a.status == 200, f"HTTP {a.status}")
    l.check(
        "Protocol=ActiveSync, Url ends with /Microsoft-Server-ActiveSync",
        isinstance(j, dict) and j.get("Protocol") == "ActiveSync" and str(j.get("Url", "")).lower().endswith("/microsoft-server-activesync"),
        f"Keys: {','.join(sorted(j)) if isinstance(j, dict) else '-'}",
    )
    l.check("Url starts with the base URL", isinstance(j, dict) and str(j.get("Url", "")).lower().startswith(c.base.lower()), "")
    a_path = get(f"/autodiscover/autodiscover.json/v1.0/{quote(mail)}?Protocol=ActiveSync")
    l.check("Path form /v1.0/<address> returns the same", a_path.status == 200 and a_path.json() == j, f"HTTP {a_path.status}")
    a2 = get(q(other, "ActiveSync"))
    l.check("other local part of the same domain: identical answer (no account oracle)", a2.status == a.status and a2.json() == j, f"HTTP {a2.status}")
    a3 = get(q(mail, "AutodiscoverV1"))
    j3 = a3.json()
    l.check(
        "JSON AutodiscoverV1 -> 200 with Url .../autodiscover/autodiscover.xml",
        a3.status == 200 and isinstance(j3, dict) and str(j3.get("Url", "")).lower().endswith("/autodiscover/autodiscover.xml"),
        f"HTTP {a3.status}, Protocol={j3.get('Protocol') if isinstance(j3, dict) else '-'}",
    )
    a4 = get(q(mail, "Foo"))
    j4 = a4.json()
    l.check(
        "unknown protocol -> 400 InvalidProtocol",
        a4.status == 400 and isinstance(j4, dict) and j4.get("ErrorCode") == "InvalidProtocol",
        f"HTTP {a4.status}, ErrorCode={j4.get('ErrorCode') if isinstance(j4, dict) else '-'}",
    )
    l.check("value 'Foo' is not echoed", b"Foo" not in a4.payload)
    a5 = get(q(foreign, "ActiveSync"))
    l.check("foreign domain -> 404", a5.status == 404, f"HTTP {a5.status}")
    all_ = [a, a_path, a2, a3, a4, a5]
    l.check("no WWW-Authenticate line (anonymous, never Bearer)", not any(x.all("www-authenticate") for x in all_))

    def pox(auth: bool, address: str) -> Reply:
        header = {"Content-Type": "text/xml; charset=utf-8", "User-Agent": AGENT}
        if auth:
            header["Authorization"] = basic(c.user, c.password())
        r = http_.request("POST", "/autodiscover/autodiscover.xml", header, POX_REQUEST.format(mail=address).encode("utf-8"))
        c.log(f"{moment()}  POX mobilesync {'with' if auth else 'without'} login  HTTP {r.status}")
        return r

    r = pox(False, mail)
    text = r.payload.decode("utf-8", "replace")
    l.check("POX mobilesync anonymous -> 200", r.status == 200, f"HTTP {r.status}")
    l.check("POX response names MobileSync and .../Microsoft-Server-ActiveSync", "MobileSync" in text and "microsoft-server-activesync" in text.lower())
    l.check("POX response contains the requested address (account-neutral)", mail in text)
    l.check("POX anonymous without WWW-Authenticate", not r.all("www-authenticate"))
    r2 = pox(True, mail)
    l.check("POX mobilesync authentifiziert -> 200", r2.status == 200, f"HTTP {r2.status}")
    r3 = pox(False, foreign)
    l.hint(f"POX mobilesync for a foreign domain, anonymous: HTTP {r3.status} (the protocol does not define this)")


def t3_provision(g: Device, l: Run, args) -> None:
    def prov(*kind_):
        return g.eas("Provision", K("Provision:Provision", K("Provision:Policies", K("Provision:Policy", *kind_))))

    a = prov(K("Provision:PolicyType", text="MS-EAS-Provisioning-WBXML"))
    w = a.root
    ok1 = l.check("Phase 1 -> HTTP 200 with WBXML", a.status == 200 and w is not None, f"HTTP {a.status}")
    if not ok1:
        return
    pol = "Provision:Policies/Provision:Policy/"
    l.check("Phase 1: Provision-Status 1", p_status(a, "Provision:Status") == 1, f"Status {txt(w, 'Provision:Status')}")
    l.check("Phase 1: Policy-Status 1", p_status(a, pol + "Provision:Status") == 1, f"Status {txt(w, pol + 'Provision:Status')}")
    l.check("Phase 1: PolicyType MS-EAS-Provisioning-WBXML", txt(w, pol + "Provision:PolicyType") == "MS-EAS-Provisioning-WBXML")
    k1 = txt(w, pol + "Provision:PolicyKey")
    l.check("Phase 1: temporary PolicyKey (decimal number)", bool(k1) and k1.isdigit(), f"length {len(k1 or '')}")
    doc = path(w, pol + "Provision:Data/Provision:EASProvisionDoc")
    l.check("Phase 1: EASProvisionDoc vorhanden", doc is not None)
    l.check("Phase 1: policy is permissive (DevicePasswordEnabled 0)", txt(doc, "Provision:DevicePasswordEnabled") in ("0", None), f"Value {txt(doc, 'Provision:DevicePasswordEnabled')}")
    a = prov(K("Provision:PolicyType", text="MS-EAS-Provisioning-WBXML"), K("Provision:PolicyKey", text=k1 or "0"), K("Provision:Status", text="1"))
    w = a.root
    l.check("Phase 2: Provision-Status 1", a.status == 200 and p_status(a, "Provision:Status") == 1, f"HTTP {a.status}, Status {txt(w, 'Provision:Status')}")
    l.check("Phase 2: Policy-Status 1", p_status(a, pol + "Provision:Status") == 1)
    k2 = txt(w, pol + "Provision:PolicyKey")
    l.check("Phase 2: final PolicyKey present", bool(k2) and k2.isdigit())
    a = g.eas("Provision", K("Provision:Provision", K("Provision:Policies", K("Provision:Policy"))))
    l.check("Provision without PolicyType -> Provision status 2", a.status == 200 and p_status(a, "Provision:Status") == 2, f"HTTP {a.status}, Status {txt(a.root, 'Provision:Status')}")
    if k2:
        g.policy_key = k2
        a, _ = g.foldersync("0")
        l.check("A command after Provision with X-MS-PolicyKey works", a.status == 200 and p_status(a, "FolderHierarchy:Status") == 1, f"HTTP {a.status}")
        g.policy_key = None
    new = g.derived("np")
    a, folder = new.foldersync("0")
    l.check("FolderSync WITHOUT a previous Provision works (no enforcement, never 449)", a.status == 200 and p_status(a, "FolderHierarchy:Status") == 1, f"HTTP {a.status}, {len(folder)} folders")
    a, _ = g.foldersync("0")
    l.check("Main device: FolderSync 0 (after Provision) -> 200", a.status == 200)


def t4_foldersync(g: Device, l: Run, args) -> None:
    a, folder = g.foldersync("0")
    if not l.check("FolderSync 0 -> HTTP 200, Status 1", a.status == 200 and p_status(a, "FolderHierarchy:Status") == 1, f"HTTP {a.status}"):
        return
    w = a.root
    key = txt(w, "FolderHierarchy:SyncKey")
    types = sorted(o["type"] for o in folder)
    fixed = {2: "Inbox", 3: "Drafts", 4: "Deleted Items", 5: "Sent Items", 6: "Outbox", 7: "Tasks", 9: "Contacts"}
    l.check("SyncKey present, <= 64 characters", bool(key) and len(key) <= 64, f"length {len(key or '')}")
    l.check(
        "Count equals the number of Add nodes",
        number(txt(w, "FolderHierarchy:Changes/FolderHierarchy:Count")) == len(folder),
        f"Count {txt(w, 'FolderHierarchy:Changes/FolderHierarchy:Count')}, Add {len(folder)}",
    )
    l.check("the seven fixed folder types 2,3,4,5,6,7,9 are present", all(t in types for t in fixed), f"Typen {types}")
    cal = [o for o in folder if o["type"] in (8, 13)]
    l.check("at least one calendar (type 8 or 13), exactly one of type 8", len(cal) >= 1 and sum(1 for o in cal if o["type"] == 8) == 1, f"{len(cal)} calendars")
    l.check("all ParentId = 0", all(o["parent"] == "0" for o in folder))
    if len(folder) != 9:
        l.hint(f"{len(folder)} folders instead of the 9 named in the architecture (7 fixed + 2 calendars)")
    a2, folder2 = g.foldersync("0")
    l.check(
        "Repetition (SyncKey 0): the same folder set and the same key", a2.status == 200 and {o["id"] for o in folder2} == {o["id"] for o in folder} and txt(a2.root, "FolderHierarchy:SyncKey") == key
    )
    a3, folder3 = g.foldersync(key)
    l.check(
        "unchanged hierarchy: status 1, Count 0, the same key",
        a3.status == 200
        and p_status(a3, "FolderHierarchy:Status") == 1
        and number(txt(a3.root, "FolderHierarchy:Changes/FolderHierarchy:Count")) in (0, None)
        and not folder3
        and txt(a3.root, "FolderHierarchy:SyncKey") == key,
    )
    a4, _ = g.foldersync("999-" + (key or "x")[:20])
    l.check("foreign/invalid key -> status 9", a4.status == 200 and p_status(a4, "FolderHierarchy:Status") == 9, f"Status {txt(a4.root, 'FolderHierarchy:Status')}")
    g.foldersync("0")


def t5_settings(g: Device, l: Run, args) -> None:
    w = K(
        "Settings:Settings",
        K("Settings:Oof", K("Settings:Get", K("Settings:BodyType", text="TEXT"))),
        K(
            "Settings:DeviceInformation",
            K("Settings:Set", K("Settings:Model", text="TestClientModell"), K("Settings:FriendlyName", text="TestClient"), K("Settings:OS", text="TestOS 1.0"), K("Settings:UserAgent", text=AGENT)),
        ),
        K("Settings:UserInformation", K("Settings:Get")),
    )
    a = g.eas("Settings", w)
    r = a.root
    if not l.check("Settings (Get Oof, Set DeviceInformation, Get UserInformation) -> 200", a.status == 200 and r is not None, f"HTTP {a.status}"):
        return
    l.check("Gesamtstatus 1", p_status(a, "Settings:Status") == 1, f"Status {txt(r, 'Settings:Status')}")
    l.check("Oof: Status 1", p_status(a, "Settings:Oof/Settings:Status") == 1, f"Status {txt(r, 'Settings:Oof/Settings:Status')}")
    l.check("Oof is off (OofState 0)", txt(r, "Settings:Oof/Settings:Get/Settings:OofState") in ("0", None), f"OofState {txt(r, 'Settings:Oof/Settings:Get/Settings:OofState')}")
    l.check("DeviceInformation: Status 1", p_status(a, "Settings:DeviceInformation/Settings:Status") == 1, f"Status {txt(r, 'Settings:DeviceInformation/Settings:Status')}")
    l.check("UserInformation: Status 1", p_status(a, "Settings:UserInformation/Settings:Status") == 1)
    sd = search(path(r, "Settings:UserInformation"), "Settings:SendDisabled")
    l.check("UserInformation: Accounts/Account/SendDisabled = 1", bool(sd) and sd[0].text == "1", f"SendDisabled {sd[0].text if sd else '-'}")
    a = g.eas("Settings", K("Settings:Settings", K("Settings:Oof", K("Settings:Set", K("Settings:OofState", text="0")))))
    l.check("Set Oof is rejected (overall status 7)", a.status == 200 and p_status(a, "Settings:Status") == 7, f"HTTP {a.status}, Status {txt(a.root, 'Settings:Status')}")


def all_folder(g: Device) -> list[dict]:
    return sorted(g.folder_list().values(), key=lambda o: (o["type"] in (8, 13), o["id"]))


def t6_sync_null(g: Device, l: Run, args) -> None:
    folder = all_folder(g)
    empty = 0
    for o in folder:
        r = g.sync(o["id"], "0")
        cal = o["type"] in (8, 13)
        ok = r.status == 1 and bool(r.key) and r.key != "0" and len(r.key) <= 64 and not r.has_commands
        l.check(
            f"Sync 0 folder '{o['id'] if not cal else o['name']}' (Typ {o['type']}): status 1, new key, no content",
            ok,
            f"Status {r.status}, key length {len(r.key or '')}, Commands {'ja' if r.has_commands else 'nein'}",
        )
        if ok and not cal:
            r2 = g.sync(o["id"], r.key, window=10, get_changes=True)
            l.check(
                f"  Follow-up Sync of empty folder '{o['id']}': status 1, another key, empty",
                r2.status == 1 and (r2.empty or bool(r2.key)) and not r2.has_commands and not r2.more,
                f"Status {r2.status}",
            )
            empty += 1
        g.st["sync"].pop(o["id"], None)
    g.ctx.state.store()
    l.hint(f"{empty} empty folders and {len(folder) - empty} calendars checked")


def sync_full_output(g: Device, l: Run, cid: str, name: str, window: int) -> Full:
    v = g.sync_full(cid, window=window)
    l.check(f"Sync paging '{name}' (WindowSize {window}) until MoreAvailable is gone, without errors", not v.error, "; ".join(v.error) or f"{v.requests} requests")
    l.check("every item exactly once (no duplicate Add)", v.double == 0, f"{len(v.elements)} items, duplicates {v.double}")
    l.check(f"no window larger than {window}", all(n <= window for n in v.window), f"largest window {max(v.window) if v.window else 0}")
    l.hint(
        f"{len(v.elements)} items, {len(v.window)} windows, {v.series} series (Recurrence), {v.private} with Sensitivity=2, "
        f"{sum(1 for e in v.elements.values() if e['all_day'])} all-day, {len(v.with_marker)} eigene Testmarken, {v.duration:.1f} s, largest response {v.bytes_max} B"
    )
    return v


def t7_sync_full(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    v = sync_full_output(g, l, o["id"], o["name"], args.window)
    r = g.sync(o["id"], v.key, window=args.window, get_changes=True)
    l.check("afterwards another Sync returns no change", r.status == 1 and r.count == 0 and not r.more, f"Status {r.status}, {r.count} changes")
    if r.key and r.key != v.key:
        l.hint("The key grew despite an empty Sync (the same counter is expected)")
    if r.key:
        g.st["sync"][o["id"]] = r.key
        g.ctx.state.store()


def t8_add(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    r, client, marker, sid = g.add_appointment(cid, all_day=getattr(args, "all_day", False))
    l.check("Sync with Add -> status 1", r.status == 1, f"Status {r.status}")
    resp = [x for x in r.r_add if x[0] == client]
    l.check("Responses/Add with our ClientId, ServerId and status 1", len(resp) == 1 and resp[0][2] == 1 and bool(resp[0][1]), f"{len(r.r_add)} responses, status {[x[2] for x in resp]}")
    l.check("new SyncKey after the Add", bool(r.key))
    if sid:
        l.check("our own Add does not come back as a server change", sid not in [s for s, _ in r.adds + r.changes], "")
        r2 = g.sync(cid, g.st["sync"][cid], window=100, get_changes=True)
        l.check("Follow-up Sync: appointment not delivered again", r2.status == 1 and sid not in [s for s, _ in r2.adds + r2.changes], f"{r2.count} changes")
        if r2.key:
            g.st["sync"][cid] = r2.key
            g.ctx.state.store()
        l.hint(
            f"Test appointment '{marker}' in '{o['name']}' created (ServerId length {len(sid)}); cross-check on the CalDAV server: "
            f"appointment with subject {marker} in the test calendar   (expected: appointment present, X-DAVBRIDGE-CREATED-BY, no ATTENDEE)"
        )


def test_change_result(l: Run, r: SyncResult, sid: str, expected: int, text: str) -> None:
    st = [s for i, s in r.r_change if i == sid]
    # success => no Responses line; error statuses appear in Responses/Change
    l.check(text, r.status == 1 and (st == [expected] or (expected == 1 and st == [])), f"Sync status {r.status}, Change-Status {st}")


def t9_change(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    t = g.last_test(cid)
    if not t:
        _, _, marker, sid = g.add_appointment(cid)
        t = g.last_test(cid)
        if not t:
            l.check("Create a test appointment for the Change", False)
            return
        l.hint(f"No test appointment known; '{marker}' created anew")
    sid, marker = t["sid"], t["marker"]
    new = marker + "-g"
    r = g.change_appointment(cid, sid, new, location="DAVBRIDGE-TEST-Location2", reminder=30, note="DAVBRIDGE-TEST-Note2")
    test_change_result(l, r, sid, 1, "Change of our own appointment -> Responses/Change status 1")
    l.check("our own Change does not come back as a server change", sid not in [s for s, _ in r.changes + r.adds])
    g.remember_test(cid, sid, new, t.get("client"))
    v = g.sync_full(cid, silent=True)
    l.check("Full sync shows the new marker", v.elements.get(sid, {}).get("marker") == new, "")
    # conflict with a second device: the CalDAV side changes between the delivery and the client Change
    b = g.derived("b")
    _, _, m2, sid2 = g.add_appointment(cid, marker=new_marker("-k"))
    if not l.check("Conflict test: second test appointment created", bool(sid2)):
        return
    vb = b.sync_full(cid, silent=True)
    l.check("Conflict test: second device (...b) sees the appointment", sid2 in vb.elements)
    rb = b.change_appointment(cid, sid2, m2 + "-B")
    test_change_result(l, rb, sid2, 1, "Conflict test: Change by the second device -> status 1")
    ra = g.sync_commands(cid, [K("AirSync:Change", K("AirSync:ServerId", text=sid2), appointment(m2 + "-A"))])
    st = [s for i, s in ra.r_change if i == sid2]
    l.check("Change on an outdated version -> Responses/Change status 7", st == [7], f"Change-Status {st}")
    srv = [ad for s, ad in ra.changes if s == sid2]
    l.check("Server version comes in the same response as Commands/Change", len(srv) == 1, f"{len(ra.changes)} Changes")
    l.check("Server version is the version of the second device (CalDAV wins)", bool(srv) and txt(srv[0], "Calendar:Subject") == m2 + "-B")
    rd = g.delete_appointment(cid, sid2)
    l.check("Cleanup: conflict appointment deleted", rd.status == 1 and all(s == 1 for _, s in rd.r_delete), f"Status {rd.status}")
    b.st["sync"].pop(cid, None)


def t10_delete(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    t = g.last_test(cid)
    if not t:
        g.add_appointment(cid)
        t = g.last_test(cid)
        if not t:
            l.check("Create a test appointment for the Delete", False)
            return
        l.hint("No test appointment known; created a new one")
    sid = t["sid"]
    r = g.delete_appointment(cid, sid)
    l.check("Delete of our own appointment -> Sync status 1", r.status == 1, f"Status {r.status}")
    l.check("Responses/Delete (if present) with status 1", all(s == 1 for _, s in r.r_delete), f"{r.r_delete and [s for _, s in r.r_delete]}")
    v = g.sync_full(cid, silent=True)
    l.check("Full sync: appointment is gone (CalDAV trash)", sid not in v.elements)
    # Delete conflict
    b = g.derived("b")
    _, _, m2, sid2 = g.add_appointment(cid, marker=new_marker("-d"))
    if not l.check("Conflict test: second test appointment created", bool(sid2)):
        return
    l.check("Conflict test: second device sees the appointment", sid2 in b.sync_full(cid, silent=True).elements)
    rb = b.change_appointment(cid, sid2, m2 + "-B")
    test_change_result(l, rb, sid2, 1, "Conflict test: Change by the second device -> status 1")
    ra = g.sync_commands(cid, [K("AirSync:Delete", K("AirSync:ServerId", text=sid2))])
    st = [s for i, s in ra.r_delete if i == sid2]
    l.check("Delete on an outdated version -> Responses/Delete status 7", st == [7], f"Delete-Status {st}")
    srv = [ad for s, ad in ra.changes + ra.adds if s == sid2]
    l.check("Server version comes back as Commands/Change or Add", len(srv) == 1 and txt(srv[0], "Calendar:Subject") == m2 + "-B", f"{len(ra.changes)} Changes")
    rd = g.delete_appointment(cid, sid2)
    l.check("immediate repetition of the rejected Delete is rejected once more (loop breaker)", rd.status == 1 and [s for _, s in rd.r_delete] == [7], f"Delete-Status {[s for _, s in rd.r_delete]}")
    rd = g.delete_appointment(cid, sid2)
    l.check("a third identical Delete succeeds (status 1 or no response line)", rd.status == 1 and all(s == 1 for _, s in rd.r_delete), f"Status {rd.status}")
    l.check("Full sync: this appointment is gone too", sid2 not in g.sync_full(cid, silent=True).elements)
    b.st["sync"].pop(cid, None)


def t11_retry(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    key = g.key(cid)
    marker, client = new_marker("-r"), uuid.uuid4().hex[:16]
    root = Device.sync_root(key, cid, commands=[K("AirSync:Add", K("AirSync:ClientId", text=client), appointment(marker, uid=uuid.uuid4().hex.upper()))])
    raw = encode(root)
    a1 = g.eas("Sync", raw=raw)
    l.hint("first response is discarded (lost response), identical request with the same SyncKey follows")
    a2 = g.eas("Sync", raw=raw)
    if not l.check("both requests -> HTTP 200 with WBXML", a1.root is not None and a2.root is not None, f"HTTP {a1.status}/{a2.status}"):
        return
    r1, r2 = SyncResult(a1), SyncResult(a2)
    l.check("both Sync status 1", r1.status == 1 and r2.status == 1, f"Status {r1.status}/{r2.status}")
    l.check("identical response structure and values", signature(a1.root) == signature(a2.root))
    l.check("same new SyncKey in both responses", bool(r1.key) and r1.key == r2.key)
    s1 = [(c, st) for c, s, st in r1.r_add]
    s2 = [(c, st) for c, s, st in r2.r_add]
    l.check("Responses to the repeated Add are identical: same ClientId, same ServerId, status 1", len(r1.r_add) == 1 and r1.r_add == r2.r_add and s1[0][1] == 1, f"{s1} / {s2}")
    if a1.payload != a2.payload:
        l.hint("bytes of the two responses are not identical (structure and values are equal)")
    sid = r1.r_add[0][1] if r1.r_add else None
    if r2.key:
        g.st["sync"][cid] = r2.key
        g.ctx.state.store()
    v = g.sync_full(cid, silent=True)
    hit = [s for s, e in v.elements.items() if e["marker"] == marker]
    l.check("Full sync: exactly one appointment with this marker (no duplicate)", len(hit) == 1, f"{len(hit)} Treffer")
    for s in hit:
        g.delete_appointment(cid, s)
    if sid and sid not in hit:
        l.hint("ServerId of the response is not in the full sync")


def t12_key(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    k0 = g.key(cid)
    r1, _, m1, s1 = g.add_appointment(cid, marker=new_marker("-s1"))
    k1 = g.st["sync"][cid]
    r2, _, m2, s2 = g.add_appointment(cid, marker=new_marker("-s2"))
    k2 = g.st["sync"][cid]
    l.check("Preparation: two Adds, key chain k0 -> k1 -> k2", bool(s1) and bool(s2) and len({k0, k1, k2}) == 3, f"{len({k0, k1, k2})} different keys")
    a = g.sync(cid, k1, window=7, get_changes=True)
    l.check("previous key with a different body -> status 3", a.status == 3, f"Status {a.status}")
    a = g.sync(cid, k0, window=100, get_changes=True)
    l.check("two keys back -> status 3", a.status == 3, f"Status {a.status}")
    broken = k2[:-2] + ("AA" if not k2.endswith("AA") else "BB")
    a = g.sync(cid, broken, window=100, get_changes=True)
    l.check("tampered key -> status 3", a.status == 3, f"Status {a.status}")
    a = g.sync(cid, "1", window=100, get_changes=True)
    l.check("invented key '1' -> status 3", a.status == 3, f"Status {a.status}")
    a = g.sync(cid, k2, window=100, get_changes=True)
    l.check("the current key keeps working afterwards (status 1)", a.status == 1, f"Status {a.status}")
    g.sync_full(cid, silent=True)
    for s in (s1, s2):
        if s:
            g.delete_appointment(cid, s)
    rest = [s for s in g.sync_full(cid, silent=True).elements if s in (s1, s2)]
    l.check("Cleanup: both test appointments deleted", not rest, f"{len(rest)} left")


def t14_ping(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    hb = getattr(args, "heartbeat", 60)
    g.sync_full(cid, silent=True)
    l.hint(f"Ping with HeartbeatInterval {hb} s beginnt {moment()}; meanwhile nothing may change in the calendar '{o['name']}' passieren")
    a, p = g.ping([cid], hb)
    l.log(f"{moment()}  Ping ended after {a.duration:.1f} s, Status {p['status']}")
    if a.status != 200:
        l.check("Ping -> 200", False, f"HTTP {a.status}")
        return
    if 60 <= hb <= 540:
        if p["status"] == 2:
            l.check("Ping without change -> status 1", False, f"Status 2 after {a.duration:.1f} s (calendar change during the Ping; repeat the run)")
            return
        l.check("Ping without change -> status 1", p["status"] == 1, f"Status {p['status']}")
        l.check("duration = HeartbeatInterval ± 2 s", abs(a.duration - hb) <= 2.5, f"{a.duration:.1f} s at {hb} s")
    else:
        allowed = 60 if hb < 60 else 540
        l.check("heartbeat out of range -> status 5", p["status"] == 5, f"Status {p['status']}")
        l.check(f"status 5 names the allowed value {allowed}", p["hb"] == allowed, f"HeartbeatInterval {p['hb']}")


def t14_limits(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    a, p = g.ping([cid], 30)
    l.check("Ping with 30 s -> status 5 with HeartbeatInterval 60", a.status == 200 and p["status"] == 5 and p["hb"] == 60, f"HTTP {a.status}, Status {p['status']}, Value {p['hb']}")
    l.check("Status 5 kommt sofort (< 5 s)", a.duration < 5, f"{a.duration:.1f} s")
    a, p = g.ping([cid], 600)
    l.check("Ping with 600 s -> status 5 with HeartbeatInterval 540", a.status == 200 and p["status"] == 5 and p["hb"] == 540, f"Status {p['status']}, Value {p['hb']}")
    a, p = g.ping(["does-not-exist"], 60)
    l.check("unknown folder ID -> status 7", a.status == 200 and p["status"] == 7, f"Status {p['status']}")
    ids = [cid, "inbox", "drafts", "deleteditems", "sentitems"]
    a, p = g.ping(ids, 60)
    l.check("five folder IDs (below the limit of 50) -> status 1", a.status == 200 and p["status"] == 1, f"Status {p['status']}, MaxFolders {p['max']}")


def t15_ping_replaces(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    pg = g.derived("p")
    pg.sync_full(cid, silent=True)
    res: dict = {}

    def first():
        res["a"] = pg.ping([cid], 60)

    t = threading.Thread(target=first, daemon=True)
    t.start()
    time.sleep(3)
    l.hint(f"second Ping of the same device replaces the first ({moment()}); the second then runs ~60 s to the end")
    a2, p2 = pg.ping([cid], 60)
    t.join(timeout=5)
    if not l.check("first Ping has ended", "a" in res):
        return
    a1, p1 = res["a"]
    l.check("first Ping ends with status 1", a1.status == 200 and p1["status"] == 1, f"Status {p1['status']}")
    l.check("first Ping ends right after the replacement (< 10 s)", a1.duration < 10, f"{a1.duration:.1f} s")
    l.check("second Ping ends normally with status 1 after ~60 s", p2["status"] == 1 and abs(a2.duration - 60) <= 3, f"Status {p2['status']}, {a2.duration:.1f} s")
    pg.st["sync"].pop(cid, None)


def t13_wake_call(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    g.sync_full(cid, silent=True)
    execution = None
    if args.trigger:
        l.hint(f"Trigger (second actor) is started: {args.trigger}")
        try:
            pr = subprocess.run(shlex.split(args.trigger), cwd=PROJECT, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            l.check("trigger started", False, type(e).__name__)
            return
        l.check("trigger ended with exit code 0", pr.returncode == 0, f"Exit {pr.returncode}")
        m = re.search(r"ausfuehrung_utc=(\S+)", pr.stdout)
        if m:
            try:
                execution = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                l.hint(f"trigger reports planned execution {m.group(1)}")
            except ValueError:
                pass
    else:
        l.hint(f"In parallel, make a change in the calendar '{o['name']}', for example in a second terminal or with --trigger '<command that creates an appointment in the test calendar>'")
    t0 = now()
    a, p = g.ping([cid], max(60, args.heartbeat))
    t1 = now()
    if not l.check("Ping ends with status 2", a.status == 200 and p["status"] == 2, f"HTTP {a.status}, Status {p['status']}, after {a.duration:.1f} s"):
        return
    l.check("calendar folder ID is in the response", cid in p["ids"], f"{len(p['ids'])} IDs")
    if execution:
        lat = (t1 - execution).total_seconds()
        l.check("wake-up latency <= 30 s (end of the Ping minus execution time)", -2 <= lat <= 30, f"{lat:.1f} s")
    else:
        l.hint(f"Ping started {t0.strftime('%H:%M:%SZ')}, endete {t1.strftime('%H:%M:%SZ')} ({a.duration:.1f} s); latency = end minus time of the CalDAV change, target <= 30 s")
    r = g.sync(cid, g.key(cid), window=100, get_changes=True)
    l.check("follow-up Sync returns the change (>= 1 item)", r.status == 1 and r.count >= 1, f"Status {r.status}, {r.count} items")
    if r.key:
        g.st["sync"][cid] = r.key
        g.ctx.state.store()


def t16_read_only(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder_read_only)
    cid = o["id"]
    v = g.sync_full(cid, silent=True)
    l.hint(f"Read-only calendar '{o['name']}': {len(v.elements)} items")
    r, client, marker, sid = g.add_appointment(cid)
    resp = [x for x in r.r_add if x[0] == client]
    l.check("Add -> Responses/Add status 6, without ServerId", len(resp) == 1 and resp[0][2] == 6 and not resp[0][1], f"responses {[(x[2], bool(x[1])) for x in resp]}")
    if sid:
        l.hint("Add was unexpectedly accepted; the appointment is deleted again")
        g.delete_appointment(cid, sid)
    if not v.elements:
        l.skipped("Change/Delete: the read-only calendar contains no items")
        return
    target = next(iter(v.elements))
    r = g.sync_commands(cid, [K("AirSync:Change", K("AirSync:ServerId", text=target), K("AirSync:ApplicationData", K("Calendar:Subject", text=new_marker("-nl"))))])
    st = [s for i, s in r.r_change if i == target]
    l.check("Change -> Responses/Change Status 7", st == [7], f"Change-Status {st}")
    l.check("server version in the same response (Commands/Change)", any(s == target for s, _ in r.changes), f"{len(r.changes)} Changes")
    r = g.sync_commands(cid, [K("AirSync:Delete", K("AirSync:ServerId", text=target))])
    st = [s for i, s in r.r_delete if i == target]
    l.check("Delete -> Responses/Delete Status 7", st == [7], f"Delete-Status {st}")
    v2 = g.sync_full(cid, silent=True)
    l.check("item is present unchanged afterwards", target in v2.elements and len(v2.elements) >= len(v.elements) - 0)


def t17_series(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    l.hint(
        "Preparation by the second actor: put the fixtures tests/fixtures/ics/imported-series.ics and imported-exception.ics into the test calendar "
        f"'{o['name']}' of the account {g.ctx.user} with a direct PUT (test account only), then run this command."
    )
    r, client, marker, sid = g.add_appointment(cid, series=True)
    resp = [x for x in r.r_add if x[0] == client]
    l.check("Add with Recurrence -> Responses/Add status 6, without ServerId", len(resp) == 1 and resp[0][2] == 6 and not resp[0][1], f"responses {[(x[2], bool(x[1])) for x in resp]}")
    if sid:
        g.delete_appointment(cid, sid)
    v = g.sync_full(cid, silent=True)
    series = {s: e for s, e in v.elements.items() if e["series"]}
    if not series:
        l.skipped("no series in the calendar (fixtures missing): reading/Change/Delete of series cannot be checked")
        return
    l.check("at least one series with Recurrence delivered", True, f"{len(series)} series")
    with_exc = {s: e for s, e in series.items() if e["exceptions"]}
    l.check("at least one series with Exceptions (EXDATE/RECURRENCE-ID)", bool(with_exc), f"{len(with_exc)} series with exceptions, {sum(e['exceptions'] for e in with_exc.values())} Ausnahmen")
    target = next(iter(series))
    r = g.sync_commands(cid, [K("AirSync:Change", K("AirSync:ServerId", text=target), K("AirSync:ApplicationData", K("Calendar:Subject", text=new_marker("-se"))))])
    st = [s for i, s in r.r_change if i == target]
    l.check("Change of a series -> status 7", st == [7], f"Change-Status {st}")
    r = g.sync_commands(cid, [K("AirSync:Delete", K("AirSync:ServerId", text=target))])
    st = [s for i, s in r.r_delete if i == target]
    l.check("Delete of a series -> status 7", st == [7], f"Delete-Status {st}")
    l.check("series present unchanged afterwards", target in g.sync_full(cid, silent=True).elements)


def mime_part() -> bytes:
    return (
        "From: test-user@example.invalid\r\nTo: nobody@example.invalid\r\nSubject: DAVBRIDGE-TEST-Abgelehnt\r\nMIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\nDAVBRIDGE-TEST-Test\r\n"
    ).encode("ascii")


def t18_rejected(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    hk = g.st.get("hier_key", "0")

    def mail(name: str, *extra):
        return K(f"ComposeMail:{name}", K("ComposeMail:ClientId", text=uuid.uuid4().hex[:12]), K("ComposeMail:SaveInSentItems"), *extra, K("ComposeMail:Mime", data=mime_part()))

    for name, root in (
        ("SendMail", mail("SendMail")),
        ("SmartReply", mail("SmartReply", K("ComposeMail:Source", K("ComposeMail:FolderId", text=cid), K("ComposeMail:ItemId", text="1")))),
        ("SmartForward", mail("SmartForward", K("ComposeMail:Source", K("ComposeMail:FolderId", text=cid), K("ComposeMail:ItemId", text="1")))),
    ):
        a = g.eas(name, root)
        s = search(a.root, "ComposeMail:Status")
        value = number(s[0].text) if s else None
        l.check(f"{name} rejected (no 500, body status 167)", a.status == 200 and value in (167, 130), f"HTTP {a.status}, Status {value}")
        if value == 130:
            l.hint(f"{name}: status 130 (AccessDenied) instead of 167; permitted fallback")
    a = g.eas(
        "MeetingResponse",
        K(
            "MeetingResponse:MeetingResponse",
            K("MeetingResponse:Request", K("MeetingResponse:UserResponse", text="1"), K("MeetingResponse:CollectionId", text=cid), K("MeetingResponse:RequestId", text="1:1")),
        ),
    )
    st = [number(r_.text) for r_ in search(a.root, "MeetingResponse:Status")]
    l.check("MeetingResponse -> Result Status 2", a.status == 200 and st[-1:] == [2], f"HTTP {a.status}, Status {st}")
    a = g.eas("MoveItems", K("Move:MoveItems", K("Move:Move", K("Move:SrcMsgId", text="1:1"), K("Move:SrcFldId", text=cid), K("Move:DstFldId", text="inbox"))))
    st = [number(r_.text) for r_ in search(a.root, "Move:Status")]
    l.check("MoveItems -> Response Status 2", a.status == 200 and st[-1:] == [2], f"HTTP {a.status}, Status {st}")
    a = g.eas(
        "FolderCreate",
        K(
            "FolderHierarchy:FolderCreate",
            K("FolderHierarchy:SyncKey", text=hk),
            K("FolderHierarchy:ParentId", text="0"),
            K("FolderHierarchy:DisplayName", text="DAVBRIDGE-TEST-Folder"),
            K("FolderHierarchy:Type", text="12"),
        ),
    )
    l.check(
        "FolderCreate type 12 (that is how Microsoft's app creates a mail folder during setup) -> status 1 with ServerId and new key",
        a.status == 200 and p_status(a, "FolderHierarchy:Status") == 1 and bool(txt(a.root, "FolderHierarchy:ServerId")) and bool(txt(a.root, "FolderHierarchy:SyncKey")),
        f"HTTP {a.status}, Status {txt(a.root, 'FolderHierarchy:Status')}",
    )
    new_id, new_key = txt(a.root, "FolderHierarchy:ServerId"), txt(a.root, "FolderHierarchy:SyncKey")
    if new_id and new_key:
        a = g.eas("FolderDelete", K("FolderHierarchy:FolderDelete", K("FolderHierarchy:SyncKey", text=new_key), K("FolderHierarchy:ServerId", text=new_id)))
        l.check("delete the created test folder again -> status 1", a.status == 200 and p_status(a, "FolderHierarchy:Status") == 1, f"Status {txt(a.root, 'FolderHierarchy:Status')}")
        g.foldersync()
        hk = g.st.get("hier_key", hk)
    a = g.eas(
        "FolderCreate",
        K(
            "FolderHierarchy:FolderCreate",
            K("FolderHierarchy:SyncKey", text=hk),
            K("FolderHierarchy:ParentId", text="0"),
            K("FolderHierarchy:DisplayName", text="DAVBRIDGE-TEST-Calendar"),
            K("FolderHierarchy:Type", text="13"),
        ),
    )
    l.check("FolderCreate calendar (type 13) -> status 3", a.status == 200 and p_status(a, "FolderHierarchy:Status") == 3, f"HTTP {a.status}, Status {txt(a.root, 'FolderHierarchy:Status')}")
    a = g.eas("FolderDelete", K("FolderHierarchy:FolderDelete", K("FolderHierarchy:SyncKey", text=hk), K("FolderHierarchy:ServerId", text="inbox")))
    l.check("FolderDelete -> Status 3", a.status == 200 and p_status(a, "FolderHierarchy:Status") == 3, f"HTTP {a.status}, Status {txt(a.root, 'FolderHierarchy:Status')}")
    a = g.eas(
        "FolderUpdate",
        K(
            "FolderHierarchy:FolderUpdate",
            K("FolderHierarchy:SyncKey", text=hk),
            K("FolderHierarchy:ServerId", text="inbox"),
            K("FolderHierarchy:ParentId", text="0"),
            K("FolderHierarchy:DisplayName", text="DAVBRIDGE-TEST-Neu"),
        ),
    )
    l.check("FolderUpdate -> Status 2", a.status == 200 and p_status(a, "FolderHierarchy:Status") == 2, f"HTTP {a.status}, Status {txt(a.root, 'FolderHierarchy:Status')}")
    a = g.eas(
        "Search",
        K(
            "Search:Search",
            K("Search:Store", K("Search:Name", text="Mailbox"), K("Search:Query", K("Search:FreeText", text="DAVBRIDGE-TEST-Suche")), K("Search:Options", K("Search:Range", text="0-9"))),
        ),
    )
    total = search(a.root, "Search:Total")
    l.check(
        "Search -> Status 1, Store-Status 1, Total 0",
        a.status == 200 and p_status(a, "Search:Status") == 1 and p_status(a, "Search:Response/Search:Store/Search:Status") == 1 and bool(total) and total[0].text == "0",
        f"HTTP {a.status}, Status {txt(a.root, 'Search:Status')}, Total {total[0].text if total else '-'}",
    )
    a = g.eas("ResolveRecipients", K("ResolveRecipients:ResolveRecipients", K("ResolveRecipients:To", text="nobody@example.invalid")))
    st = [number(r_.text) for r_ in search(kind(a.root, "ResolveRecipients:Response"), "ResolveRecipients:Status")]
    l.check(
        "ResolveRecipients -> overall status 1, status 4 per recipient",
        a.status == 200 and p_status(a, "ResolveRecipients:Status") == 1 and st[:1] == [4],
        f"HTTP {a.status}, Status {txt(a.root, 'ResolveRecipients:Status')}, recipient {st}",
    )


def t19_unknown(g: Device, l: Run, args) -> None:
    for cmd in ("GetAttachment", "ValidateCert", "Find"):
        a = g.eas(cmd)
        l.check(f"unadvertised command {cmd} -> 501", a.status == 501, f"HTTP {a.status}")
    a = g.eas("Frobnicate")
    l.check("completely unknown command name -> 501 or 400, never 500", a.status in (400, 501), f"HTTP {a.status}")
    if a.status != 501:
        l.hint(f"unknown command name returns HTTP {a.status} (501 for unadvertised commands, 400 for codes without a table entry)")
    good = K("FolderHierarchy:FolderSync", K("FolderHierarchy:SyncKey", text="0"))
    for v in ("12.1", "16.1", "2.5"):
        a = g.eas("FolderSync", good, version=v)
        l.check(f"wrong protocol version {v} -> 400", a.status == 400, f"HTTP {a.status}")
    broken = {
        "truncated (STR_I without end)": bytes.fromhex("03016a00000756520330"),
        "Zufallsbytes": b"\xff\xfe\xfd\xfc garbage\x00\x01",
        "Attribut-Bit gesetzt": bytes.fromhex("03016a0000075680") + b"\x01\x01",
        "Literal-Tag": bytes.fromhex("03016a00000744") + b"\x01",
        "header only, string table != 0": bytes.fromhex("03016a05000756010101"),
    }
    for name, raw in broken.items():
        a = g.eas("FolderSync", raw=raw)
        l.check(f"kaputtes WBXML ({name}) -> 400", a.status == 400, f"HTTP {a.status}")
    a = g.eas("FolderSync", good, ctype="text/plain")
    l.check("wrong Content-Type -> 400", a.status == 400, f"HTTP {a.status}")
    large = HEADER + b"\x00" * (1_200_000)
    a = g.eas("FolderSync", raw=large, timeout=120)
    l.check("body too large (1.2 MB) -> 413", a.status == 413, f"HTTP {a.status}")
    a = g.eas("FolderSync", good, auth=False)
    l.check("POST without login -> 401 (Basic only)", a.status == 401 and all(w.lower().startswith("basic") for w in a.all("www-authenticate")) and bool(a.all("www-authenticate")), f"HTTP {a.status}")
    a, _ = g.foldersync("0")
    l.check("afterwards DAVbridge works normally (FolderSync -> 200)", a.status == 200 and p_status(a, "FolderHierarchy:Status") == 1)


def t20_password(g: Device, l: Run, args) -> None:
    for i in range(3):
        a = g.options(password=f"wrong-{uuid.uuid4().hex[:10]}")
        l.check(f"wrong password {i + 1}/3 -> 401 (Basic only)", a.status == 401 and all(w.lower().startswith("basic") for w in a.all("www-authenticate")), f"HTTP {a.status}")
    a = g.options()
    l.check("correct password afterwards -> 200 (counter < 10)", a.status == 200, f"HTTP {a.status}")


def t21_lock(g: Device, l: Run, args) -> None:
    if not args.confirm_lock:
        l.hint("SECOND ACTOR/FOLLOW-UP: this test deliberately locks your own IP in the EAS namespace (15 minutes). Confirm with --confirm-lock.")
        l.skipped("not executed without --confirm-lock")
        return
    for i in range(10):
        a = g.options(password=f"wrong-{uuid.uuid4().hex[:10]}")
        if a.status not in (401, 503):
            l.check(f"Fehlversuch {i + 1}/10 -> 401", False, f"HTTP {a.status}")
            return
    a = g.options()
    ra = a.h("retry-after")
    l.check("11th request -> 503", a.status == 503, f"HTTP {a.status}")
    l.check("Retry-After names seconds ≤ 900", bool(ra) and ra.isdigit() and 0 < int(ra) <= 900, f"Retry-After {ra}")
    l.check("the lock reports no Bearer/Negotiate", not any(w.lower().startswith(("bearer", "negotiate")) for w in a.all("www-authenticate")))
    ews = (
        '<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
        'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"><soap:Header><t:RequestServerVersion Version="Exchange2016"/></soap:Header><soap:Body><m:GetFolder>'
        '<m:FolderShape><t:BaseShape>IdOnly</t:BaseShape></m:FolderShape><m:FolderIds><t:DistinguishedFolderId Id="calendar"/></m:FolderIds></m:GetFolder></soap:Body></soap:Envelope>'
    )
    r = g.ctx.http.request("POST", "/EWS/Exchange.asmx", {"Authorization": basic(g.ctx.user, g.ctx.password()), "Content-Type": "text/xml; charset=utf-8", "User-Agent": AGENT}, ews.encode("utf-8"))
    g.ctx.log(f"{moment()}  EWS GetFolder     HTTP {r.status}")
    l.check("Mac/EWS path stays untouched (GetFolder with Basic -> 200)", r.status == 200, f"HTTP {r.status}")
    l.hint("Lift it: davbridge unblock --ip eas:<IP from the log>  (or --all); otherwise wait ~15 minutes")


def read_rss_mib(container: str) -> float | None:
    try:
        pr = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.match(r"\s*([\d.]+)\s*([KMGk]?i?B)", pr.stdout or "")
    if not m:
        return None
    factor = {"B": 1 / 1048576, "KiB": 1 / 1024, "kB": 1 / 1024, "KB": 1 / 1024, "MiB": 1, "MB": 1, "GiB": 1024, "GB": 1024}.get(m.group(2))
    return float(m.group(1)) * factor if factor else None


def t22_last(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    if args.rss_measure and not args.container:
        raise Error("--rss-measure braucht --container <Name>")
    l.hint(f"Load test: full sync of '{o['name']}' (expected: a test calendar with many appointments)")
    peak: list[float] = []
    stop = threading.Event()

    def measure():
        while not stop.is_set():
            m = read_rss_mib(args.container)
            if m is not None:
                peak.append(m)
            stop.wait(0.2)

    th = None
    if args.rss_measure:
        th = threading.Thread(target=measure, daemon=True)
        th.start()
    try:
        v = sync_full_output(g, l, cid, o["name"], args.window)
    finally:
        stop.set()
        if th:
            th.join(timeout=35)
    l.check(f"duration < {args.max_duration} s", v.duration < args.max_duration, f"{v.duration:.1f} s")
    l.check("0 errors", not v.error)
    if args.expect_series is not None:
        l.check(f"{args.expect_series} series delivered", v.series == args.expect_series, f"{v.series}")
    if args.expect_private is not None:
        l.check(f"{args.expect_private} private appointments with Sensitivity=2", v.private == args.expect_private, f"{v.private}")
    if args.expect_count is not None:
        l.check(f"{args.expect_count} appointments", len(v.elements) == args.expect_count, f"{len(v.elements)}")
    if args.rss_measure:
        if peak:
            l.check(f"Spitzen-RSS < {args.rss_limit} MiB (docker stats, {len(peak)} Messungen)", max(peak) < args.rss_limit, f"{max(peak):.0f} MiB")
        else:
            l.hint("RSS not measurable (docker stats not available)")
    else:
        l.hint("RSS not measured; use --rss-measure --container <name> or in parallel: docker stats <container>")


def t23_simultaneously(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    l.hint("SECOND ACTOR: run the EWS streaming of the Mac in parallel, for example 'tools/stream_probe.sh --http1.1 5' (expected: events keep arriving within 25 s, no ErrorServerBusy).")
    ok = True
    for i in range(args.repeats):
        v = g.sync_full(o["id"], window=args.window, silent=True)
        l.log(f"{moment()}  Full sync {i + 1}/{args.repeats}: {len(v.elements)} items, {v.duration:.1f} s")
        ok = ok and not v.error and v.double == 0
    l.check("all full syncs without errors and without 503", ok, f"{args.repeats} runs")


def t24_restart(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    key = g.key(cid)
    proc = None
    res: dict = {}

    def ping():
        res["a"] = g.ping([cid], 300)
        res["ende"] = now()

    if not args.trigger:
        l.hint(
            f"SECOND ACTOR: while the Ping runs (from {moment()}, up to 300 s) restart the DAVbridge container (this costs the EWS subscriptions, "
            "announce it first). With --trigger '<restart command>' the client does it itself."
        )
    th = threading.Thread(target=ping, daemon=True)
    th.start()
    t_from = None
    if args.trigger:
        time.sleep(args.delay)
        t_from = now()
        l.hint(f"trigger starts {t_from.strftime('%H:%M:%SZ')}: {args.trigger}")
        proc = subprocess.Popen(shlex.split(args.trigger), cwd=PROJECT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    th.join(timeout=360)
    if not l.check("Ping has ended", "a" in res):
        return
    a, p = res["a"]
    l.check("Ping ends with status 1", a.status == 200 and p["status"] == 1, f"HTTP {a.status}, Status {p['status']}, after {a.duration:.1f} s")
    if t_from:
        lat = (res["ende"] - t_from).total_seconds()
        l.check("Ping ends within 5 s of the restart request (expected <= 1 s after SIGTERM)", 0 <= lat <= 5, f"{lat:.1f} s")
    if proc:
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            pass
    ready = False
    for _ in range(45):
        try:
            if g.options().status == 200:
                ready = True
                break
        except Error:
            pass
        time.sleep(2)
    l.check("service reachable again (OPTIONS 200)", ready)
    if ready:
        r = g.sync(cid, key, window=100, get_changes=True)
        l.check("follow-up Sync with the old valid key works (the state is in SQLite)", r.status == 1, f"Status {r.status}")
        if r.key:
            g.st["sync"][cid] = r.key
            g.ctx.state.store()


def tx1_extra(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    v = g.sync_full(cid, silent=True)
    key = g.st["sync"][cid]

    def estimate(k: str, c: str) -> Reply:
        return g.eas(
            "GetItemEstimate",
            K("GetItemEstimate:GetItemEstimate", K("GetItemEstimate:Collections", K("GetItemEstimate:Collection", K("AirSync:SyncKey", text=k), K("GetItemEstimate:CollectionId", text=c)))),
        )

    est = lambda a: p_status(a, "GetItemEstimate:Response/GetItemEstimate:Status")  # noqa: E731
    a = estimate(key, cid)
    l.check("GetItemEstimate with a valid key -> status 1", a.status == 200 and est(a) == 1, f"HTTP {a.status}, Status {est(a)}")
    a = estimate("0", cid)
    l.check("GetItemEstimate with SyncKey 0 (not prepared) -> status 3", a.status == 200 and est(a) == 3, f"Status {est(a)}")
    a = estimate(key, "does-not-exist")
    l.check("GetItemEstimate with an invalid folder -> status 2", a.status == 200 and est(a) == 2, f"Status {est(a)}")
    a = estimate(key[:-2] + "AA", cid)
    l.check("GetItemEstimate with a tampered key -> status 4", a.status == 200 and est(a) == 4, f"Status {est(a)}")

    def fetch(sid: str) -> Reply:
        return g.eas(
            "ItemOperations",
            K("ItemOperations:ItemOperations", K("ItemOperations:Fetch", K("ItemOperations:Store", text="Mailbox"), K("AirSync:ServerId", text=sid), K("AirSync:CollectionId", text=cid))),
        )

    if v.elements:
        sid = next(iter(v.elements))
        a = fetch(sid)
        fs = number(txt(path(a.root, "ItemOperations:Response"), "ItemOperations:Fetch/ItemOperations:Status"))
        l.check(
            "ItemOperations Fetch of a known appointment -> overall status 1, Fetch status 1",
            a.status == 200 and p_status(a, "ItemOperations:Status") == 1 and fs == 1,
            f"HTTP {a.status}, Status {txt(a.root, 'ItemOperations:Status')}, Fetch {fs}",
        )
    else:
        l.skipped("Fetch known appointment: calendar empty")
    a = fetch("000000000000")
    fs = number(txt(path(a.root, "ItemOperations:Response"), "ItemOperations:Fetch/ItemOperations:Status"))
    l.check("ItemOperations Fetch of an unknown ServerId -> Fetch status 6", a.status == 200 and fs == 6, f"HTTP {a.status}, Fetch {fs}")
    a = g.eas("ItemOperations", K("ItemOperations:ItemOperations", K("ItemOperations:EmptyFolderContents", K("AirSync:CollectionId", text="deleteditems"))))
    l.check("ItemOperations EmptyFolderContents -> Gesamtstatus 2", a.status == 200 and p_status(a, "ItemOperations:Status") == 2, f"HTTP {a.status}, Status {txt(a.root, 'ItemOperations:Status')}")
    a, _ = g.foldersync("0")


def clean_up(g: Device, l: Run, args) -> None:
    o = g.calendar(args.folder)
    cid = o["id"]
    v = g.sync_full(cid, silent=True)
    remains = list(v.with_marker)
    deleted = 0
    for i in range(0, len(remains), 50):
        r = g.sync_commands(cid, [K("AirSync:Delete", K("AirSync:ServerId", text=s)) for s in remains[i : i + 50]])
        if r.status == 1:
            deleted += len(remains[i : i + 50])
    g.st["tests"][cid] = []
    g.ctx.state.store()
    l.check(f"{len(remains)} appointments with marker '{MARKER}' in '{o['name']}' deleted", True, f"{deleted} confirmed")
    if remains:
        v2 = g.sync_full(cid, silent=True)
        l.check("no marker appointments left afterwards", not v2.with_marker, f"{len(v2.with_marker)} left")


# identifier -> (title, function, default-folder argument)
SCENARIOS = {
    "T1": ("OPTIONS with/without login", t1_options),
    "T2": ("Autodiscover JSON and POX", t2_autodiscover),
    "T3": ("Provision phase 1/2 and commands without Provision", t3_provision),
    "T4": ("FolderSync", t4_foldersync),
    "T5": ("Settings", t5_settings),
    "T6": ("Sync 0 for all folders", t6_sync_null),
    "T7": ("Sync paging until MoreAvailable is gone", t7_sync_full),
    "T8": ("Add of a test appointment", t8_add),
    "T9": ("Change (correct and conflict)", t9_change),
    "T10": ("Delete (correct and conflict)", t10_delete),
    "T11": ("Retry with the same SyncKey", t11_retry),
    "T12": ("old/tampered SyncKey", t12_key),
    "T13": ("Ping wake-up by a CalDAV change", t13_wake_call),
    "T14": ("Ping without change, and limits", None),
    "T15": ("Ping replaced", t15_ping_replaces),
    "T16": ("Read-only calendar", t16_read_only),
    "T17": ("Read/write series", t17_series),
    "T18": ("rejected commands", t18_rejected),
    "T19": ("unknown command, version, WBXML, size", t19_unknown),
    "T20": ("wrong password 3x", t20_password),
    "T21": ("Lockout after 10 failed attempts", t21_lock),
    "T22": ("Lasttest Vollabgleich", t22_last),
    "T23": ("EAS full sync in parallel to the Mac streaming", t23_simultaneously),
    "T24": ("Restart in the middle of a Ping", t24_restart),
    "X1": ("GetItemEstimate, Fetch, EmptyFolderContents", tx1_extra),
}
# `all`: automatic = without a second actor; otherwise SKIP with a hint to the dedicated subcommand
SKIP_IN_EVERYTHING = {
    "T13": "second actor: wake --trigger '<command that creates an appointment in the test calendar within 10 s>'",
    "T17": "second actor: put the series fixtures into the test calendar with a direct PUT, then run the series scenario",
    "T21": "locks your own IP for 15 minutes: the lockout scenario with --confirm-lock",
}
EVERYTHING_ORDER = ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "T10", "T11", "T12", "T13", "T14", "T15", "T16", "T17", "T18", "T19", "T20", "T21", "X1"]


def run(ctx: Context, identifier: str, args, g: Device | None = None) -> str:
    title, fn = SCENARIOS[identifier]
    l = Run(ctx, identifier, title)
    l.header()
    g = g or Device(ctx, ctx.device)
    try:
        if identifier == "T14":
            t14_ping(g, l, args)
            t14_limits(g, l, args)
        else:
            fn(g, l, args)
    except PasswordError:
        raise
    except Error as e:
        l.check("run without abort", False, str(e))
    except WbxmlError as e:
        l.check("response readable as WBXML", False, str(e))
    except Exception as e:  # noqa: BLE001 - every scenario reports instead of crashing
        l.check("run without an unexpected exception", False, f"{type(e).__name__}")
    ctx.p(f"-- {identifier} {l.verdict}")
    return l.verdict


def everything(ctx: Context, args) -> int:
    results: dict[str, tuple[str, str]] = {}
    g = Device(ctx, ctx.device)
    for k in EVERYTHING_ORDER:
        title = SCENARIOS[k][0]
        if k in SKIP_IN_EVERYTHING:
            ctx.p(f"== {k} {title}\n  [skipped] {SKIP_IN_EVERYTHING[k]}\n-- {k} SKIP")
            results[k] = ("SKIP", title)
            continue
        if args.fast and k in ("T14", "T15"):
            ctx.p(f"== {k} {title}\n  [skipped] --fast (the Ping scenarios take about 1 to 2 minutes each)\n-- {k} SKIP")
            results[k] = ("SKIP", title)
            continue
        results[k] = (run(ctx, k, args, g), title)
    # cleanup of the own test markers
    l = Run(ctx, "AUFR", "Cleanup of the test markers")
    l.header()
    try:
        o = g.calendar(args.folder)
        v = g.sync_full(o["id"], silent=True)
        if v.with_marker:
            clean_up(g, l, args)
        else:
            l.check("no leftover test markers in the calendar", True)
    except (Error, WbxmlError) as e:
        l.hint(f"Cleanup not possible: {e}")
    ctx.p("")
    ctx.p("== Result ==")
    for k in EVERYTHING_ORDER:
        verdict, title = results[k]
        extra = f"   ({SKIP_IN_EVERYTHING[k]})" if verdict == "SKIP" and k in SKIP_IN_EVERYTHING else ""
        ctx.p(f"  {k:<4} {verdict:<5} {title}{extra}")
    n = {u: sum(1 for x, _ in results.values() if x == u) for u in ("PASS", "FAIL", "SKIP")}
    ctx.p(f"Total: PASS {n['PASS']}, FAIL {n['FAIL']}, SKIP {n['SKIP']}   ({moment()})")
    return 1 if n["FAIL"] else 0


# ---------------------------------------------------------------- CLI


def parser() -> argparse.ArgumentParser:
    per = argparse.ArgumentParser(add_help=False)
    per.add_argument("--user", required=True, help="Account (Basic user) of a test account")
    per.add_argument("--url", required=True, help="Base URL of the DAVbridge instance, e.g. https://dav.example.com")
    per.add_argument("--password-stdin", action="store_true", required=True, help="Read the password as one line from stdin (never as an argument)")
    per.add_argument("--device", default="testclient01", help="Device ID [A-Za-z0-9]{1,32}, default testclient01")
    per.add_argument("--folder", default="Test Calendar", help="Calendar (ID or part of the display name), default 'Test Calendar'")
    per.add_argument("--folder-read-only", default="Read-Only Calendar", help="Read-only calendar for T16, default 'Read-Only Calendar'")
    per.add_argument("--state", default=None, help="State file (SyncKeys, ServerIds), default ~/.cache/eas-testclient/<user>.json (0600)")
    per.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout in seconds (Ping adds the heartbeat)")
    per.add_argument("--tls-insecure", action="store_true", help="Do not verify the certificate (local experiments only)")
    per.add_argument("--quiet", action="store_true", help="no line per request")
    per.add_argument("-v", "--verbose", action="store_true", help="Print the response skeleton (structure/enumeration values only) per request")
    p = argparse.ArgumentParser(
        prog="eas_test_client.py",
        description="EAS test client (protocol 14.1) for DAVbridge. Password: one line from stdin (--password-stdin).",
        epilog="Scenarios: T1 options, T2 autodiscover, T3 provision, T4 foldersync, T5 settings, T6 sync-null, T7 sync-full, T8 add, T9 change, T10 delete, "
        "T11 retry, T12 keys, T13 wake*, T14 ping, T15 ping-replaced, T16 readonly, T17 series*, T18 rejected, T19 unknown, T20 password, "
        "T21 lockout*, T22 load, T23 concurrent*, T24 restart*.  (* = needs a second actor; `all` skips T13/T17/T21 with SKIP)",
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def new(name, help):
        return sub.add_parser(name, parents=[per], help=help, description=help)

    new("options", "T1: OPTIONS with/without login (401 Basic only; 200 with 14.1 and 17 commands)")
    new("autodiscover", "T2: autodiscover.json (ActiveSync/AutodiscoverV1/unknown/foreign domain) and POX mobilesync anonymous/authenticated")
    new("provision", "T3: Provision phase 1/2 and commands without Provision")
    new("foldersync", "T4: FolderSync 0, repetition, unchanged hierarchy, invalid key (9)")
    new("settings", "T5: Settings (Oof, DeviceInformation, UserInformation); Set Oof -> 7")
    new("sync-null", "T6: Sync 0 for all folders")
    s = new("sync-full", "T7: Sync 0, then paging with --window until MoreAvailable is gone; counts items, series, Sensitivity=2")
    s.add_argument("--window", type=int, default=3, help="WindowSize (Standard 3)")
    s = new("add", "T8: create a test appointment DAVBRIDGE-TEST-... (stays in the state for change/delete)")
    s.add_argument("--all-day", action="store_true", help="create an all-day appointment (needs zoneinfo)")
    new("change", "T9: change the test appointment; then a conflict (the second device changes first) -> status 7 + server version")
    new("delete", "T10: delete the test appointment; then a Delete conflict -> status 7")
    new("retry", "T11: the same Add request twice with the same SyncKey -> identical response, exactly one appointment")
    new("keys", "T12: old key with a different body and tampered key -> status 3")
    s = new("wake", "T13 (second actor): Ping open, a change on the CalDAV server in parallel -> status 2 within 30 s")
    s.add_argument("--heartbeat", type=int, default=60)
    s.add_argument("--trigger", default=None, help="Command of the second actor, for example one that creates an appointment in the test calendar within 10 s")
    s = new("ping", "T14: one Ping with --heartbeat, measures the time until the status (60..540 -> 1; otherwise 5)")
    s.add_argument("--heartbeat", type=int, default=60)
    new("ping-limits", "T14 (fast): heartbeat 30 -> 5/60, 600 -> 5/540, unknown folder ID -> 7, five folders -> 1 (limit 50)")
    new("ping-replaced", "T15: a second Ping of the same device ends the first with status 1 (takes about 1 minute)")
    new("readonly", "T16: read-only calendar: Add 6, Change 7, Delete 7")
    new("series", "T17 (second actor: fixtures): read series, Change/Delete 7, Add with Recurrence 6")
    new("rejected", "T18: SendMail/SmartReply/SmartForward/MeetingResponse/MoveItems/Folder*/Search/ResolveRecipients")
    new("unknown", "T19: unknown command (501), wrong version (400), broken WBXML (400), body too large (413)")
    new("password", "T20: wrong password 3x -> 401, then the correct one -> 200")
    s = new("lockout", "T21 (locks your own IP!): 10 wrong passwords, 11th request -> 503 + Retry-After; EWS path untouched")
    s.add_argument("--confirm-lock", action="store_true", help="Confirmation that your own IP may be locked in the EAS namespace")
    s = new("load", "T22: full sync of the test calendar: duration, errors, series, private items, optionally RSS")
    s.add_argument("--window", type=int, default=100)
    s.add_argument("--max-duration", type=float, default=30.0)
    s.add_argument("--expect-series", type=int, default=None)
    s.add_argument("--expect-private", type=int, default=None)
    s.add_argument("--expect-count", type=int, default=None)
    s.add_argument("--rss-measure", action="store_true", help="docker stats <--container> every second (needs Docker permissions)")
    s.add_argument("--container", default=None, help="Container name for --rss-measure")
    s.add_argument("--rss-limit", type=float, default=200.0)
    s = new("concurrent", "T23 (second actor: Mac streaming): repeated full syncs")
    s.add_argument("--window", type=int, default=100)
    s.add_argument("--repeats", type=int, default=3)
    s = new("restart", "T24 (second actor: restart): Ping open, restart DAVbridge -> status 1 within 1 s, follow-up Sync valid")
    s.add_argument("--trigger", default=None, help="for example 'docker restart -t 20 <container>' (this costs the EWS subscriptions!)")
    s.add_argument("--delay", type=float, default=10.0, help="Seconds from the start of the Ping until the trigger")
    new("extra", "X1: GetItemEstimate, ItemOperations Fetch/EmptyFolderContents")
    new("cleanup", "delete all appointments with subject DAVBRIDGE-TEST-... in the calendar")
    new("clear-state", "Remove the state file")
    s = new("all", "T1 to T21 in sequence with PASS/FAIL/SKIP; exit code 1 on FAIL")
    s.add_argument("--window", type=int, default=3, help="WindowSize for T7 (default 3)")
    s.add_argument("--fast", action="store_true", help="Skip T14/T15 (long Pings)")
    return p


SUBCOMMANDS = {
    "options": "T1",
    "autodiscover": "T2",
    "provision": "T3",
    "foldersync": "T4",
    "settings": "T5",
    "sync-null": "T6",
    "sync-full": "T7",
    "add": "T8",
    "change": "T9",
    "delete": "T10",
    "retry": "T11",
    "keys": "T12",
    "wake": "T13",
    "ping-replaced": "T15",
    "readonly": "T16",
    "series": "T17",
    "rejected": "T18",
    "unknown": "T19",
    "password": "T20",
    "lockout": "T21",
    "load": "T22",
    "concurrent": "T23",
    "restart": "T24",
    "extra": "X1",
}


def main(argv: list[str] | None = None, http_obj: Http | None = None, stdin=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    args = parser().parse_args(argv)
    try:
        ctx = Context(args, http_obj, stdin)
        if args.command == "clear-state":
            ctx.state.delete()
            ctx.p("State deleted")
            return 0
        if args.command == "all":
            return everything(ctx, args)
        g = Device(ctx, ctx.device)
        if args.command == "ping":
            l = Run(ctx, "T14", "Ping without change (one Ping)")
            l.header()
            try:
                t14_ping(g, l, args)
            except Error as e:
                l.check("run without abort", False, str(e))
            ctx.p(f"-- T14 {l.verdict}")
            return 1 if l.verdict == "FAIL" else 0
        if args.command == "ping-limits":
            l = Run(ctx, "T14", "Ping limits")
            l.header()
            try:
                t14_limits(g, l, args)
            except Error as e:
                l.check("run without abort", False, str(e))
            ctx.p(f"-- T14 {l.verdict}")
            return 1 if l.verdict == "FAIL" else 0
        if args.command == "cleanup":
            l = Run(ctx, "AUFR", "Cleanup of the test markers")
            l.header()
            clean_up(g, l, args)
            return 1 if l.fail_n else 0
        verdict = run(ctx, SUBCOMMANDS[args.command], args, g)
        return 1 if verdict == "FAIL" else 0
    except Error as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
