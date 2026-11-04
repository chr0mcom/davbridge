"""`davbridge check`: read-only diagnosis of an installation (configuration, state directory, CalDAV server, users, public endpoints).

Nothing is written outside a throw-away probe file in the state directory, no calendar content is read and no secret is ever part of a
finding. Network access goes through an optional `httpx` transport so that every probe can be mocked. Findings carry a level; the exit code
is 0 without findings above `info`, 1 with warnings and 2 with errors that prevent operation.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import platform
import shutil
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx

from . import __version__
from .caldav import CaldavError, CaldavKind, Credentials, HttpCaldav
from .clock import SystemClock
from .config import (
    ALLOW_INSECURE_HTTP_VAR,
    Account,
    Config,
    ConfigError,
    account_for,
    apply_environment,
    check_start,
    dynamic_account,
    from_dict,
    load_raw,
    public_representation,
    read_key,
    renamed_dynamic_account,
)
from .discovery import DiscoveryError, choose_default
from .identifiers import IdService
from .store import SCHEMA_VERSION, DatabaseError, Verdict, inspect_database, refusal_message

SCHEMA_VERSION_OF_REPORT = 1
HMAC_KEY_FILE = "hmac.key"
DATABASE_FILE = "davbridge.sqlite"
LOW_DISK_BYTES = 100 * 1024 * 1024
Level = Literal["ok", "info", "warning", "error"]
DEPENDENCIES = ("fastapi", "starlette", "uvicorn", "httpx", "lxml", "icalendar")
INTERNET_NOTE = "A request from this host does not prove that the service is reachable from the internet; test from outside as well."


@dataclass(frozen=True, slots=True)
class Finding:
    id: str
    level: Level
    message: str


@dataclass(slots=True)
class Report:
    findings: list[Finding] = field(default_factory=list)
    effective_config: dict | None = None

    def add(self, level: Level, id: str, message: str) -> None:
        self.findings.append(Finding(id, level, message))

    @property
    def exit_code(self) -> int:
        levels = {f.level for f in self.findings}
        return 2 if "error" in levels else 1 if "warning" in levels else 0

    @property
    def result(self) -> str:
        return {0: "ok", 1: "warning", 2: "error"}[self.exit_code]

    def as_json(self) -> dict:
        document: dict = {
            "schema_version": SCHEMA_VERSION_OF_REPORT,
            "version": __version__,
            "result": self.result,
            "exit_code": self.exit_code,
            "findings": [{"id": f.id, "level": f.level, "message": f.message} for f in self.findings],
        }
        if self.effective_config is not None:
            document["effective_config"] = self.effective_config
        return document

    def as_text(self) -> str:
        labels = {"ok": "ok   ", "info": "info ", "warning": "WARN ", "error": "FAIL "}
        lines = [f"[{labels[f.level]}] {f.message}" for f in self.findings]
        warnings = sum(f.level == "warning" for f in self.findings)
        errors = sum(f.level == "error" for f in self.findings)
        lines.append(f"Result: {self.result} ({errors} error(s), {warnings} warning(s))")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class CheckOptions:
    config_path: Path
    environment: Mapping[str, str]
    passwords: Mapping[str, str] = field(default_factory=dict, repr=False)
    public: bool = False
    show_effective_config: bool = False
    transport: httpx.AsyncBaseTransport | None = None


def _client(config: Config, options: CheckOptions, *, verify: bool) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=options.transport,
        timeout=httpx.Timeout(config.read_timeout_seconds, connect=config.connect_timeout_seconds),
        verify=verify,
        follow_redirects=False,
    )


def _host(url: str) -> str:
    return urlsplit(url).netloc


def _network_problem(error: httpx.HTTPError) -> str:
    cause = error.__cause__ or error.__context__
    text = str(error).lower()
    if isinstance(cause, ssl.SSLError) or "certificate" in text or "ssl" in text:
        return "TLS verification failed (certificate chain, host name or expiry)"
    if isinstance(error, httpx.TimeoutException):
        return "the connection timed out"
    return "the connection failed (DNS, routing, firewall or the service is down)"


# ------------------------------------------------------------------ configuration


def _check_configuration(options: CheckOptions, report: Report) -> Config | None:
    environment = options.environment
    try:
        raw = load_raw(options.config_path)
        apply_environment(raw, environment)
        config = from_dict(raw, environment)
        check_start(config)
    except ConfigError as e:
        report.add("error", "config.invalid", f"Configuration {options.config_path}: {e}")
        return None
    explicit = sum(not a.auto for a in config.accounts)
    dynamic = sum(a.auto for a in config.accounts)
    report.add("ok", "config.valid", f"Configuration is valid: {options.config_path} ({explicit} user(s) with explicit calendars, {dynamic} with autodiscovery, auth.mode = {config.auth_mode})")
    if options.show_effective_config:
        report.effective_config = public_representation(config)
    if config.auth_mode == "pattern":
        report.add(
            "warning",
            "config.pattern-mode",
            f'auth.mode = "pattern": ANY CalDAV user matching {config.username_pattern!r} with a valid password can use this bridge. Keep the pattern as narrow as possible.',
        )
    if not config.verify_tls:
        report.add("warning", "config.tls-verification-disabled", "caldav.verify_tls = false: the CalDAV server certificate is not verified. Use this for development only.")
    if environment.get(ALLOW_INSECURE_HTTP_VAR) == "true":
        report.add("warning", "config.insecure-http", f"{ALLOW_INSECURE_HTTP_VAR}=true allows plain-HTTP URLs. Use this for development only.")
    if config.follow_redirects:
        report.add("warning", "config.follow-redirects", "caldav.follow_redirects = true: redirects from the CalDAV server are followed (autodiscovery refuses them regardless).")
    if not config.trusted_proxies:
        report.add(
            "info",
            "config.trusted-proxies",
            "server.trusted_proxies is empty: forwarded client addresses are ignored. Behind a reverse proxy all clients then share the proxy address (one lock-out counter).",
        )
    else:
        report.add("ok", "config.trusted-proxies", f"Forwarded headers are trusted from {len(config.trusted_proxies)} proxy network(s).")
    if config.ews_enabled:
        report.add("ok", "protocol.ews", "EWS (Outlook for Mac) is enabled.")
    else:
        report.add("info", "protocol.ews", "EWS (Outlook for Mac) is disabled.")
    if config.eas.enabled:
        report.add("ok", "protocol.eas", "EAS 14.1 (Outlook for iOS/Android) is enabled; mobile cloud processing is acknowledged (protocols.eas.acknowledge_mobile_cloud_processing = true).")
        report.add("info", "protocol.eas.cloud", "Outlook mobile processes a copy of the calendar in Microsoft's cloud. Make sure that is acceptable for every enabled user (docs/privacy.md).")
    else:
        report.add("info", "protocol.eas", "EAS (Outlook for iOS/Android) is disabled; the mobile cloud acknowledgement is not needed.")
    return config


# ------------------------------------------------------------------ state directory


def _probe_directory(directory: Path) -> str | None:
    """Create, rename and delete a probe file. Returns an error text or None."""
    probe = directory / f".davbridge-check-{os.getpid()}"
    renamed = directory / f".davbridge-check-{os.getpid()}.renamed"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("probe")
        os.replace(probe, renamed)
        os.unlink(renamed)
    except OSError as e:
        for leftover in (probe, renamed):
            try:
                os.unlink(leftover)
            except OSError:
                pass
        return e.strerror or "I/O error"
    return None


def _check_state_directory(config: Config, report: Report, now: float) -> None:
    state = config.state_dir
    if not state.is_dir():
        report.add("error", "state.missing", f"State directory {state} does not exist or is not a directory. Create it (or run `davbridge init`) and make it writable for this user.")
        return
    stat = state.stat()
    uid, gid = os.getuid(), os.getgid()
    problem = _probe_directory(state)
    if problem:
        report.add("error", "state.not-writable", f"State directory {state} is not writable by uid {uid}:{gid} ({problem}); it is owned by {stat.st_uid}:{stat.st_gid}.")
    else:
        report.add("ok", "state.writable", f"State directory {state} is writable (create, rename and delete probe passed; running as uid {uid}:{gid}, owned by {stat.st_uid}:{stat.st_gid}).")
    if stat.st_mode & 0o077:
        report.add("warning", "state.permissions", f"State directory {state} is accessible to group or others (mode {stat.st_mode & 0o777:04o}); use 0700.")
    usage = shutil.disk_usage(state)
    if usage.free < LOW_DISK_BYTES:
        report.add("warning", "state.disk-space", f"Only {usage.free // (1024 * 1024)} MiB free on the state volume.")
    else:
        report.add("ok", "state.disk-space", f"{usage.free // (1024 * 1024)} MiB free on the state volume.")
    key_path = state / HMAC_KEY_FILE
    try:
        read_key(key_path)
    except ConfigError as e:
        report.add("error", "state.hmac-key", f"{e}.")
    else:
        report.add("ok", "state.hmac-key", f"{key_path} is present, 32 bytes, a regular file owned by uid {os.geteuid()} with mode {key_path.stat().st_mode & 0o777:04o}.")
    database = state / DATABASE_FILE
    try:
        info = inspect_database(database, now)
    except (DatabaseError, OSError) as e:
        report.add("error", "state.database", f"The state database {database} cannot be read ({type(e).__name__}).")
        return
    if not info.exists:
        report.add("info", "state.database", f"No state database yet; the service creates {database} at its first start.")
        return
    if info.integrity != "ok":
        report.add("error", "state.database", f"SQLite integrity check of {database} failed: {info.integrity[:200]}")
        return
    if info.verdict in (Verdict.NEWER, Verdict.FOREIGN):
        report.add("error", "state.schema", f"{refusal_message(database, info.verdict, info.schema_version)}.")
    elif info.verdict in (Verdict.NEW, Verdict.CURRENT, Verdict.ADOPT):
        report.add(
            "ok",
            "state.database",
            f"State database is intact (schema {SCHEMA_VERSION}, {info.size_bytes // 1024} KiB, {info.sync_states} "
            f"sync state(s), {info.devices} EAS device(s), {info.active_locks} active lock-out(s)).",
        )
    else:
        report.add(
            "warning",
            "state.schema",
            f"State database has an older schema ({info.schema_version}, this version uses {SCHEMA_VERSION}): it is discarded and recreated at the next start (see `davbridge migrate --check`).",
        )
    mode = database.stat().st_mode & 0o777
    if mode & 0o077:
        report.add(
            "warning", "state.database-permissions", f"{database} is accessible to group or others (mode {mode:04o}) and contains client addresses; it is tightened to 0600 at the next service start."
        )


def _check_runtime(report: Report) -> None:
    versions = []
    for name in DEPENDENCIES:
        try:
            versions.append(f"{name} {importlib.metadata.version(name)}")
        except importlib.metadata.PackageNotFoundError:
            versions.append(f"{name} (not installed)")
    commit = os.environ.get("GIT_COMMIT", "")
    commit_text = f", commit {commit[:12]}" if commit and commit != "unknown" else ""
    report.add("info", "runtime.version", f"DAVbridge {__version__}{commit_text}; Python {platform.python_version()}; {'; '.join(versions)}.")


# ------------------------------------------------------------------ CalDAV and users


async def _check_caldav_server(config: Config, options: CheckOptions, report: Report) -> bool:
    host = _host(config.caldav_base_url)
    try:
        async with _client(config, options, verify=config.verify_tls) as client:
            response = await client.options(config.caldav_base_url, headers={"User-Agent": "davbridge-check/1"})
    except httpx.HTTPError as e:
        report.add("error", "caldav.unreachable", f"CalDAV server {host}: {_network_problem(e)}.")
        return False
    if 300 <= response.status_code < 400:
        report.add("error", "caldav.redirect", f"CalDAV server {host} answers with a redirect ({response.status_code}); DAVbridge never follows redirects. Set caldav.base_url to the final URL.")
        return False
    if response.status_code >= 500:
        report.add("error", "caldav.unavailable", f"CalDAV server {host} answers with HTTP {response.status_code}.")
        return False
    report.add("ok", "caldav.reachable", f"CalDAV server {host} is reachable (HTTP {response.status_code}, no redirect, TLS {'verified' if config.verify_tls else 'NOT verified'}).")
    dav = response.headers.get("DAV")
    if dav is not None and "calendar-access" not in dav:
        report.add("warning", "caldav.capabilities", "The server's DAV header does not advertise calendar-access; is caldav.base_url the CalDAV endpoint?")
    return True


def _caldav_failure(e: CaldavError) -> str:
    return {
        CaldavKind.AUTH: "the server rejected the credentials",
        CaldavKind.FORBIDDEN: "access is forbidden",
        CaldavKind.NOT_FOUND: "not found",
        CaldavKind.BUSY: "the server is busy or unavailable",
        CaldavKind.NETWORK: "the connection failed",
    }.get(e.kind, f"unexpected answer ({e.kind.value}{', HTTP ' + str(e.status) if e.status else ''})")


def _account_for(config: Config, name: str) -> Account | None:
    return account_for(config, name) or dynamic_account(config, name)


async def _check_explicit_calendars(caldav: HttpCaldav, config: Config, account: Account, credentials: Credentials, report: Report) -> None:
    for cal in account.calendar:
        label = f"user {account.name}, calendar {cal.key} ({cal.uri})"
        try:
            info = await caldav.calendar_info(credentials, cal)
        except CaldavError as e:
            if e.kind == CaldavKind.NOT_FOUND:
                report.add("error", f"user.{account.name}.calendar.{cal.key}", f"{label}: the calendar does not exist for this user (check users.calendars.path).")
            else:
                report.add("error", f"user.{account.name}.calendar.{cal.key}", f"{label}: {_caldav_failure(e)}.")
            continue
        if "VEVENT" not in info.components:
            report.add("error", f"user.{account.name}.calendar.{cal.key}", f"{label}: the collection does not support events (VEVENT).")
            continue
        if cal.write == "never":
            effective = 'read-only (write = "never")'
        elif cal.owner in config.read_only_owners:
            effective = "read-only (owner is listed in features.read_only_owners)"
        elif not info.writable:
            effective = "read-only (the server grants no write privilege)"
        else:
            effective = "writable"
        report.add("ok", f"user.{account.name}.calendar.{cal.key}", f"{label}: found, supports events, effective rights: {effective}.")


async def _check_discovered_calendars(caldav: HttpCaldav, config: Config, account: Account, credentials: Credentials, ids: IdService, report: Report) -> None:
    prefix = f"user.{account.name}.discovery"
    try:
        candidates = await caldav.discover_calendars(credentials, config.dynamic.include_shared)
    except CaldavError as e:
        report.add("error", prefix, f"user {account.name}: calendar autodiscovery failed: {_caldav_failure(e)}.")
        return
    keys = {c.uri: ids.calendar_key(account.name, c.uri) for c in candidates}
    shown = ", ".join(f"{c.uri} ({'writable' if c.writable else 'read-only'}{', shared' if c.shared else ''})" for c in candidates) or "none"
    report.add(
        "info", f"{prefix}.candidates", f"user {account.name}: {len(candidates)} calendar candidate(s) (shared calendars {'included' if config.dynamic.include_shared else 'excluded'}): {shown}."
    )
    try:
        default = choose_default(candidates, keys, config.dynamic.default_calendar, config.read_only_owners)
    except DiscoveryError as e:
        if e.reason == "no-calendar":
            report.add("error", prefix, f"user {account.name}: no readable calendar with event support was found; the user cannot log in.")
        else:
            report.add(
                "error",
                prefix,
                f"user {account.name}: no default calendar can be chosen with dynamic_users.default_calendar "
                f'= "{config.dynamic.default_calendar}"; name the exact calendar path or use "first-writable".',
            )
        return
    report.add("ok", prefix, f"user {account.name}: autodiscovery works; default calendar is {default}.")


async def _check_users(config: Config, options: CheckOptions, report: Report) -> None:
    if not options.passwords:
        names = ", ".join(a.name for a in config.accounts) or "(pattern mode: no fixed users)"
        report.add("info", "users.not-tested", f"Users were not tested ({names}). Use `davbridge check --user NAME --password-stdin` to test sign-in and calendars of one user.")
        return
    try:
        key = read_key(config.state_dir / HMAC_KEY_FILE)
    except ConfigError:
        key = bytes(32)
    ids = IdService(key)
    async with _client(config, options, verify=config.verify_tls) as client:
        caldav = HttpCaldav(config.caldav_base_url, client)
        for name, password in options.passwords.items():
            account = _account_for(config, name)
            if account is None:
                report.add(
                    "error",
                    f"user.{name}.unknown",
                    f"user {name}: not allowed by the configuration (not in [[users]], auth.allowed_users or auth.username_pattern); the service refuses this login without contacting CalDAV.",
                )
                continue
            credentials = Credentials(account.name, password)
            try:
                ok = await caldav.check_access(credentials)
            except CaldavError as e:
                report.add("error", f"user.{account.name}.auth", f"user {account.name}: sign-in could not be verified: {_caldav_failure(e)}.")
                continue
            if not ok:
                report.add(
                    "error",
                    f"user.{account.name}.auth",
                    f"user {account.name}: the CalDAV server rejected the credentials (wrong password, two-factor authentication without an app password, or wrong user name).",
                )
                continue
            report.add("ok", f"user.{account.name}.auth", f"user {account.name}: signed in at the CalDAV server.")
            if account.auto and account_for(config, name) is None:
                try:
                    canonical = await caldav.canonical_user(credentials)
                except CaldavError:
                    canonical = None
                if canonical and canonical != account.name:
                    report.add("info", f"user.{account.name}.canonical", f"user {account.name}: the server knows this user as {canonical}; the service uses that spelling for state and identifiers.")
                    account = renamed_dynamic_account(config, account, canonical)
                    credentials = Credentials(account.name, password)
            if account.auto:
                await _check_discovered_calendars(caldav, config, account, credentials, ids, report)
            else:
                await _check_explicit_calendars(caldav, config, account, credentials, report)
            if account.eas and not config.eas.enabled:
                report.add("error", f"user.{account.name}.eas", f"user {account.name}: eas_enabled is set but EAS is disabled globally.")


# ------------------------------------------------------------------ public endpoints


def _basic_challenge(response: httpx.Response) -> bool:
    challenge = response.headers.get("www-authenticate", "")
    return response.status_code == 401 and challenge.lower().startswith("basic") and not any(w in challenge.lower() for w in ("bearer", "negotiate", "ntlm"))


async def _check_public(config: Config, options: CheckOptions, report: Report) -> None:
    host = _host(config.public_url)
    probes: list[tuple[str, str, str]] = []
    if config.ews_enabled:
        probes.append(("ews", "GET", "/EWS/Exchange.asmx"))
    if config.eas.enabled:
        probes.append(("eas", "OPTIONS", "/Microsoft-Server-ActiveSync"))
    async with _client(config, options, verify=True) as client:
        for name, method, path in probes:
            try:
                response = await client.request(method, config.public_url + path, headers={"User-Agent": "davbridge-check/1"})
            except httpx.HTTPError as e:
                report.add("error", f"public.{name}", f"{host}{path}: {_network_problem(e)}.")
                return
            if 300 <= response.status_code < 400:
                target = urlsplit(response.headers.get("location", "")).netloc
                report.add(
                    "error" if target and target != host else "warning", f"public.{name}", f"{host}{path} answers with a redirect ({response.status_code}); clients do not follow redirects reliably."
                )
            elif _basic_challenge(response):
                report.add("ok", f"public.{name}", f"{host}{path} answers 401 with a Basic challenge (and no Bearer/Negotiate/NTLM challenge), as expected.")
            else:
                report.add(
                    "error",
                    f"public.{name}",
                    f"{host}{path} answered HTTP {response.status_code} without a plain Basic challenge; check the reverse proxy (no SSO or portal in front, Authorization passed through).",
                )
        try:
            unknown = await client.get(config.public_url + "/davbridge-check-unknown-path", headers={"User-Agent": "davbridge-check/1"})
            if unknown.status_code != 404:
                report.add("warning", "public.unknown-path", f"An unknown path answers HTTP {unknown.status_code} instead of 404; something other than DAVbridge may answer for this host.")
            health = await client.get(config.public_url + "/healthz", headers={"User-Agent": "davbridge-check/1"})
            if health.status_code == 200:
                report.add("warning", "public.healthz", "/healthz is publicly routable; route only the Outlook paths and keep the health endpoint internal.")
            else:
                report.add("ok", "public.healthz", "/healthz is not publicly routable.")
        except httpx.HTTPError as e:
            report.add("warning", "public.unknown-path", f"{host}: {_network_problem(e)}.")
    report.add("info", "public.scope", INTERNET_NOTE)


# ------------------------------------------------------------------ entry point


async def run_check(options: CheckOptions) -> Report:
    report = Report()
    _check_runtime(report)
    config = _check_configuration(options, report)
    if config is None:
        return report
    await asyncio.to_thread(_check_state_directory, config, report, SystemClock().now())
    if await _check_caldav_server(config, options, report):
        await _check_users(config, options, report)
    if options.public:
        await _check_public(config, options, report)
    return report
