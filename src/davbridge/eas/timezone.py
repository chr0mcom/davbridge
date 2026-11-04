"""EAS time zone block (TIME_ZONE_INFORMATION, [MS-ASDTYPE] 2.6.4) <-> IANA. Pure module: no I/O, no asyncio.

Layout (172 bytes, little-endian): Bias int32 | StandardName 32xUTF-16 | StandardDate SYSTEMTIME | StandardBias int32 |
DaylightName 32xUTF-16 | DaylightDate SYSTEMTIME | DaylightBias int32. Bias in minutes, UTC = local time + Bias.
A SYSTEMTIME with wYear = 0 is a yearly rule: wDay = n-th occurrence of the weekday (5 = last).
"""

from __future__ import annotations

import base64
import binascii
import calendar
import datetime as dt
import functools
import struct
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from ..timezones import UTC_NAMES

LENGTH = 172
STEP_S = 6 * 3600
_FORMAT = "<i64s8Hi64s8Hi"
PREFERRED = (
    "Europe/Berlin",
    "UTC",
    "Europe/London",
    "Europe/Lisbon",
    "Europe/Helsinki",
    "Europe/Istanbul",
    "Europe/Moscow",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "Asia/Dubai",
    "Asia/Tokyo",
)


@dataclass(frozen=True, slots=True)
class Rule:
    month: int  # 1-12, 0 = no transition
    weekday: int  # 0 = Sunday
    occurrences: int  # 1-5, 5 = last
    hour: int
    minute: int


@dataclass(frozen=True, slots=True)
class Timezone:
    bias: int  # minutes, UTC = local + bias (Berlin: -60)
    default: Rule
    default_bias: int
    summer: Rule
    summer_bias: int  # Berlin: -60
    default_name: str = ""
    summer_name: str = ""

    def key(self) -> tuple:
        if not self.summer.month:
            return (self.bias, 0)
        return (self.bias, self.default, self.default_bias, self.summer, self.summer_bias)


NONE = Rule(0, 0, 0, 0, 0)


def _systemtime(r: Rule) -> tuple[int, ...]:
    return (0, r.month, r.weekday, r.occurrences if r.month else 0, r.hour, r.minute, 0, 0)


def _name(text: str) -> bytes:
    return text[:31].encode("utf-16-le").ljust(64, b"\x00")


def encode(z: Timezone) -> str:
    raw = struct.pack(_FORMAT, z.bias, _name(z.default_name), *_systemtime(z.default), z.default_bias, _name(z.summer_name), *_systemtime(z.summer), z.summer_bias)
    return base64.b64encode(raw).decode("ascii")


def decode(text: str) -> Timezone:
    """Base64 text -> time zone. ValueError for a wrong length or nonsensical values."""
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("time zone: not base64") from None
    if len(raw) != LENGTH:
        raise ValueError("time zone: length")
    w = struct.unpack(_FORMAT, raw)
    bias, sname, sd, sbias, dname, dd, dbias = w[0], w[1], w[2:10], w[10], w[11], w[12:20], w[20]

    def rule(st: tuple[int, ...]) -> Rule:
        _year, month, weekday, tag, hour, minute = st[:6]
        if not month:
            return NONE
        if not (1 <= month <= 12 and 0 <= weekday <= 6 and 1 <= tag <= 5 and 0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("time zone: rule")
        return Rule(month, weekday, tag, hour, minute)

    for b in (bias, sbias, dbias):
        if not -24 * 60 <= b <= 24 * 60:
            raise ValueError("time zone: bias")

    def text_from(b: bytes) -> str:
        return b.decode("utf-16-le", errors="replace").split("\x00", 1)[0]

    s, d = rule(sd), rule(dd)
    if bool(s.month) != bool(d.month):
        s, d = NONE, NONE
    return Timezone(bias, s, sbias, d, dbias, text_from(sname), text_from(dname))


def _state(zone: ZoneInfo, utc_ts: int) -> tuple[dt.timedelta, dt.timedelta]:
    local = dt.datetime.fromtimestamp(utc_ts, dt.timezone.utc).astimezone(zone)
    return local.utcoffset() or dt.timedelta(0), local.dst() or dt.timedelta(0)


def _switch_point(zone: ZoneInfo, left: int, right: int) -> int:
    outcome = _state(zone, left)
    while right - left > 1:
        middle = (left + right) // 2
        if _state(zone, middle) == outcome:
            left = middle
        else:
            right = middle
    return right


def _transitions(zone: ZoneInfo, year: int) -> list[tuple[dt.datetime, dt.timedelta, dt.timedelta]]:
    """(local time before the transition, offset after it, DST after it) for all transitions in the year."""
    start = int(dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    end = int(dt.datetime(year + 1, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    result = []
    t = start
    while t < end:
        next = min(t + STEP_S, end)
        if _state(zone, t) != _state(zone, next):
            point = _switch_point(zone, t, next)
            old = _state(zone, point - 1)
            new = _state(zone, point)
            local_old = (dt.datetime.fromtimestamp(point, dt.timezone.utc) + old[0]).replace(tzinfo=None)
            result.append((local_old, new[0], new[1]))
        t = next
    return result


def _rule_from(local: dt.datetime) -> Rule:
    days = calendar.monthrange(local.year, local.month)[1]
    occurrences = 5 if local.day + 7 > days else (local.day - 1) // 7 + 1
    return Rule(local.month, (local.weekday() + 1) % 7, occurrences, local.hour, local.minute)


@functools.lru_cache(maxsize=512)
def from_iana(iana: str, year: int) -> Timezone:
    """IANA zone -> time zone block with the rules of `year`. KeyError for an unknown zone."""
    try:
        zone = ZoneInfo(iana)
    except (ZoneInfoNotFoundError, ValueError, OSError) as error:
        raise KeyError(iana) from error
    transition = _transitions(zone, year)
    summer_change = [u for u in transition if u[2]]
    winter_change = [u for u in transition if not u[2]]
    if len(summer_change) == 1 and len(winter_change) == 1:
        default_offset = winter_change[0][1]
        summer_offset = summer_change[0][1]
        bias = -int(default_offset.total_seconds() // 60)
        return Timezone(bias, _rule_from(winter_change[0][0]), 0, _rule_from(summer_change[0][0]), -int((summer_offset - default_offset).total_seconds() // 60))
    middle = dt.datetime(year, 7, 1, tzinfo=dt.timezone.utc).astimezone(zone)
    january = dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc).astimezone(zone)
    offset = january.utcoffset() if not (january.dst() or dt.timedelta(0)) else middle.utcoffset()
    return Timezone(-int((offset or dt.timedelta(0)).total_seconds() // 60), NONE, 0, NONE, 0)


@functools.lru_cache(maxsize=8)
def _catalog(year: int) -> dict[tuple, str]:
    all = {z for z in available_timezones() if "/" in z and not z.startswith(("SystemV/", "posix/", "right/"))}
    regional = sorted(z for z in all - set(PREFERRED) if not z.startswith("Etc/"))
    candidates = list(PREFERRED) + regional + sorted(z for z in all if z.startswith("Etc/"))
    catalog: dict[tuple, str] = {}
    for iana in candidates:
        try:
            catalog.setdefault(from_iana(iana, year).key(), iana)
        except KeyError:
            continue
    return catalog


def iana_for(z: Timezone, year: int) -> str | None:
    """Matching IANA zone for a block (same rules in `year`), preferring the order in PREFERRED. None if none matches."""
    return _catalog(year).get(z.key())


def fixed_zone(z: Timezone) -> dt.timezone:
    """Fallback without a matching IANA zone: fixed offset of standard time."""
    return dt.timezone(dt.timedelta(minutes=-z.bias))


def blob_for_iana(iana: str, reference: dt.datetime) -> bytes:
    """172-byte block for `iana` with the rules of the reference year. UTC => all 0 with the name "UTC". KeyError for an unknown zone."""
    if iana in UTC_NAMES:
        return base64.b64decode(encode(Timezone(0, NONE, 0, NONE, 0, "UTC", "UTC")))
    return base64.b64decode(encode(from_iana(iana, reference.year)))


def blob_base64(iana: str, reference: dt.datetime) -> str:
    return base64.b64encode(blob_for_iana(iana, reference)).decode("ascii")


def blob_from_base64(text: str) -> bytes:
    decode(text)
    return base64.b64decode(text.strip())


def iana_for_blob(blob: bytes, reference: dt.datetime) -> str | None:
    """Matching IANA zone (preferring PREFERRED) or None; the caller then falls back to the default zone."""
    try:
        z = decode(base64.b64encode(blob).decode("ascii"))
    except ValueError:
        return None
    return iana_for(z, reference.year)


def warm_up() -> None:
    """Precompute the catalog for this and the next year (runs at startup in a thread, never in the event loop)."""
    year = dt.datetime.now(dt.timezone.utc).year
    for j in (year, year + 1):
        _catalog(j)
