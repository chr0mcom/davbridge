#!/usr/bin/env python3
"""Developer tool: create, change, delete and inspect test events with an explicitly named TEST account.

This is not part of the public `davbridge` command line. It writes to a real CalDAV server, therefore:
  - the user must be named explicitly (--user) and must be a configured user of the given configuration file,
  - only events whose title starts with DAVBRIDGE-TEST are created, changed or deleted,
  - the CalDAV (app) password is read from stdin (--password-stdin), never from an argument or the environment.

Usage examples (run from a checkout with the package importable, e.g. `pip install -e .`):
  printf '%s' "$PW" | python tools/dev_calendar.py --config davbridge.toml --user alice --password-stdin create --test DAVBRIDGE-TEST-1
  printf '%s' "$PW" | python tools/dev_calendar.py --config davbridge.toml --user alice --password-stdin check --title DAVBRIDGE-TEST
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import statistics
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from lxml import etree

from davbridge import ical
from davbridge.caldav import CaldavError, Credentials, HttpCaldav
from davbridge.clock import SystemClock
from davbridge.config import Config, ConfigError, load, read_key, runtime_default_zone, set_runtime_default_zone
from davbridge.identifiers import ElementRef, IdService
from davbridge.store import Store

TITLE_PATTERN = re.compile(r"^[A-Za-z0-9 _.:\-]{1,80}$")
TYPES_NS = "http://schemas.microsoft.com/exchange/services/2006/types"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def load_account(args) -> tuple[Config, object, Credentials]:
    config = load(Path(args.config), os.environ)
    set_runtime_default_zone(config.default_timezone)
    account = next((k for k in config.accounts if k.name == args.user), None)
    if account is None:
        raise SystemExit(f"user {args.user!r} is not configured in {args.config}")
    if not args.password_stdin:
        raise SystemExit("--password-stdin is required (the app password is read from stdin, never from an argument)")
    password = sys.stdin.readline().strip()
    if not password:
        raise SystemExit("no password on stdin")
    return config, account, Credentials(account.name, password)


def ids(config: Config) -> IdService:
    return IdService(read_key(config.state_dir / "hmac.key"))


def calendar_of(account, calendar_id: str | None):
    calendar = account.calendar_after_key(calendar_id) if calendar_id else account.default_calendar()
    if calendar is None:
        raise SystemExit("unknown calendar id")
    return calendar


def event_title(args) -> str:
    title = args.title or f"{args.test} {uuid.uuid4().hex[:6]}"
    if not TITLE_PATTERN.match(title) or not title.startswith("DAVBRIDGE-TEST"):
        raise SystemExit("the title must start with DAVBRIDGE-TEST and may contain only simple characters")
    return title


def start_time(args, now: datetime) -> datetime:
    zone = ZoneInfo(runtime_default_zone())
    try:
        day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else (now.astimezone(zone) + timedelta(days=args.days)).date()
        hour, minute = (int(x) for x in args.time.split(":"))
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).astimezone(timezone.utc)
    except ValueError:
        raise SystemExit("--date YYYY-MM-DD and --time HH:MM expected") from None


async def fetch_all(caldav: HttpCaldav, credentials: Credentials, calendar):
    delta = await caldav.sync(credentials, calendar, None)
    return await caldav.get_several(credentials, calendar, sorted(delta.changed))


async def cmd_check(args) -> int:
    config, account, credentials = load_account(args)
    calendar = calendar_of(account, args.calendar)
    async with httpx.AsyncClient(timeout=30) as http:
        objects = await fetch_all(HttpCaldav(config.caldav_base_url, http), credentials, calendar)
    hits = [(o, t) for o in objects for t in ical.read(o.ics or "") if not args.title or t.subject.startswith(args.title)]
    if args.count:
        print(f"count={len(hits)}")
        return 0
    print(f"found={'yes' if hits else 'no'} count={len(hits)} time_utc={datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
    service = ids(config)
    for o, t in hits:
        print(
            f"- href={o.href} hash={service.element_hash(account.name, ElementRef(calendar.key, o.href))} uid={t.uid} title={t.subject!r} "
            f"start={t.start:%Y-%m-%dT%H:%M:%SZ} end={t.end:%Y-%m-%dT%H:%M:%SZ} all_day={t.all_day} location={t.location!r} reminder_min={t.reminder_min}"
        )
        if args.fidelity and o.ics:
            print("  properties=" + ",".join(sorted({line.split(":", 1)[0].split(";")[0] for line in o.ics.replace("\r\n ", "").splitlines() if ":" in line})))
    return 0


async def cmd_event(args) -> int:
    if not args.test.startswith("DAVBRIDGE-TEST"):
        raise SystemExit("--test must start with DAVBRIDGE-TEST: only test events are touched")
    title = event_title(args) if args.action == "create" else None
    config, account, credentials = load_account(args)
    calendar = calendar_of(account, None)
    service = ids(config)
    store = await Store.open(config.state_dir / "davbridge.sqlite", SystemClock(), config.failure_limit, config.initial_lock_seconds)
    now = datetime.now(timezone.utc)
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            caldav = HttpCaldav(config.caldav_base_url, http)
            if args.action == "create":
                start = start_time(args, now)
                item = etree.fromstring(
                    f'<t:CalendarItem xmlns:t="{TYPES_NS}"><t:Subject>{title}</t:Subject><t:Start>{start:%Y-%m-%dT%H:%M:%SZ}</t:Start>'
                    f"<t:End>{start + timedelta(minutes=args.minutes):%Y-%m-%dT%H:%M:%SZ}</t:End><t:Location>Test location</t:Location></t:CalendarItem>"
                )
                zone = ical.ContextZone(iana=args.zone) if args.zone else ical.ContextZone()
                uid, fields = ical.read_creation(item, config.features, zone)
                uid = uid or str(uuid.uuid4())
                href = ical.href_for_uid(uid)
                await caldav.create(credentials, calendar, href, ical.new(uid, fields, now))
                digest = service.element_hash(account.name, ElementRef(calendar.key, href))
                await store.event(account.name, "change", folder=calendar.key, element=digest)
                local = start.astimezone(ZoneInfo(runtime_default_zone()))
                print(f"created href={href} hash={digest} time_utc={now:%Y-%m-%dT%H:%M:%SZ} start_local={local:%Y-%m-%d %H:%M} weekday={WEEKDAYS[local.weekday()]} duration_min={args.minutes}")
                return 0
            objects = [o for o in await fetch_all(caldav, credentials, calendar) if o.ics and any(t.subject.startswith(args.test) for t in ical.read(o.ics))]
            if not objects:
                print("no test event found")
                return 1
            for o in objects:
                digest = service.element_hash(account.name, ElementRef(calendar.key, o.href))
                if args.action == "delete":
                    await caldav.delete(credentials, calendar, o.href, o.etag)
                    await store.event(account.name, "change", folder=calendar.key, element=digest)
                    print(f"deleted hash={digest} time_utc={datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
                elif args.action == "change":
                    new_title = f"{args.test} changed {uuid.uuid4().hex[:4]}"
                    change = etree.fromstring(
                        f'<t:ItemChange xmlns:t="{TYPES_NS}"><t:ItemId Id="x"/><t:Updates><t:SetItemField><t:FieldURI FieldURI="item:Subject"/>'
                        f"<t:CalendarItem><t:Subject>{new_title}</t:Subject></t:CalendarItem></t:SetItemField></t:Updates></t:ItemChange>"
                    )
                    fields = ical.read_changes(change, config.features, ical.ContextZone())
                    await caldav.change(credentials, calendar, o.href, ical.patch(o.ics, fields, datetime.now(timezone.utc)), o.etag)
                    await store.event(account.name, "change", folder=calendar.key, element=digest)
                    print(f"changed hash={digest} title={new_title!r} time_utc={datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
            return 0
    except CaldavError as e:
        print(f"error={e.kind.value}")
        return 2
    finally:
        await store.close()


async def cmd_latency(args) -> int:
    config = load(Path(args.config), os.environ)
    store = await Store.open(config.state_dir / "davbridge.sqlite", SystemClock(), config.failure_limit, config.initial_lock_seconds)
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).timestamp()
        changes = await store.events("change", since)
        delivered = await store.events("delivered", since)
        detected = await store.events("detected", since)
        values = []
        for change in changes:
            if args.item and change[4] != args.item:
                continue
            later = [x[0] for x in delivered if x[4] == change[4] and x[0] >= change[0]]
            seen = [x[0] for x in detected if x[4] == change[4] and x[0] >= change[0]]
            if later:
                values.append(later[0] - change[0])
            print(
                f"item={change[4]} change={datetime.fromtimestamp(change[0], timezone.utc):%H:%M:%S} "
                f"detected_after_s={(seen[0] - change[0]) if seen else '-'} delivered_after_s={(later[0] - change[0]) if later else '-'}"
            )
        if values:
            values.sort()
            print(f"count={len(values)} median_s={statistics.median(values):.1f} max_s={max(values):.1f}")
        return 0
    finally:
        await store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dev_calendar.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="DAVbridge configuration file")
    parser.add_argument("--user", help="explicit test user (a [[users]] entry of the configuration)")
    parser.add_argument("--password-stdin", action="store_true", help="read the CalDAV app password from stdin")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="list events (optionally by title prefix) of a calendar")
    check.add_argument("--calendar", help="calendar id (default: the default calendar)")
    check.add_argument("--title", help="title prefix")
    check.add_argument("--count", action="store_true")
    check.add_argument("--fidelity", action="store_true", help="also list the iCalendar property names")
    check.set_defaults(run=cmd_check)
    event = sub.add_parser("event", help="create, change or delete DAVBRIDGE-TEST events in the default calendar")
    event.add_argument("action", choices=["create", "change", "delete"])
    event.add_argument("--test", required=True, help="test marker, must start with DAVBRIDGE-TEST")
    event.add_argument("--title")
    event.add_argument("--days", type=int, default=1)
    event.add_argument("--date", help="YYYY-MM-DD in the configured default_timezone, overrides --days")
    event.add_argument("--time", default="10:00", help="HH:MM local time")
    event.add_argument("--minutes", type=int, default=30)
    event.add_argument("--zone")
    event.set_defaults(run=cmd_event)
    latency = sub.add_parser("latency", help="notification latency from the state database (needs the state directory of the service)")
    latency.add_argument("--item")
    latency.add_argument("--hours", type=float, default=6)
    latency.set_defaults(run=cmd_latency)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("check", "event") and not args.user:
        raise SystemExit("--user is required: name the test account explicitly")
    try:
        return asyncio.run(args.run(args))
    except ConfigError as e:
        raise SystemExit(f"error: {e}") from None


if __name__ == "__main__":
    raise SystemExit(main())
