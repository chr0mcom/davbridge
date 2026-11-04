"""Configuration: TOML schema (config_version = 1), environment overrides and strict validation.

Internal attribute names are still the pre-translation ones; every user-visible name (TOML keys, environment
variables, error messages) is English. Immutable dataclasses.
"""

from __future__ import annotations

import base64
import errno
import ipaddress
import json
import os
import re
import stat
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import IntEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

CONFIG_VERSION = 1
DEFAULT_CONFIG_PATH = "/etc/davbridge/davbridge.toml"
DEFAULT_STATE_DIR = "/var/lib/davbridge"
ALLOW_INSECURE_HTTP_VAR = "DAVBRIDGE_ALLOW_INSECURE_HTTP"

KEY_PATTERN = re.compile(r"^[a-z0-9-]{1,40}$")
URI_PATTERN = re.compile(r"^[A-Za-z0-9%][A-Za-z0-9._@%+-]{0,199}$")
ENCODED_SEPARATOR = re.compile(r"%(?:2[FfEe]|5[Cc])")
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}$")
ADDRESS_PATTERN = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}$")
PREFIX_PATTERN = re.compile(r"^[A-Z][A-Z0-9-]*-$")
CULTURE_PATTERN = re.compile(r"^[a-z]{2,3}:[a-z]{2,3}$")


PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


def valid_calendar_segment(value: str) -> bool:
    """One path segment below the calendar home. A percent-encoded name (`my%40cal`, `%C3%A4`) stays allowed. The value is decoded exactly once
    and then refused if it contains a separator (`/`, `\\`), is a `.`/`..` segment, holds a control character or still holds a percent escape
    (`x%252F..%252Fbob`): a proxy or server that decodes a second time must never be able to walk out of the segment.
    An encoded `/`, `\\` or `.` is refused outright (`x%2F..%2Fbob%2Fcalendar`, `x%2E%2E`)."""
    if not URI_PATTERN.fullmatch(value) or ENCODED_SEPARATOR.search(value):
        return False
    try:
        decoded = unquote(value, errors="strict")
    except UnicodeDecodeError:
        return False
    if decoded in (".", "..") or "/" in decoded or "\\" in decoded or PERCENT_ESCAPE.search(decoded):
        return False
    return not any(unicodedata.category(c) == "Cc" for c in decoded)


RESERVED = frozenset(
    {
        "root",
        "msgfolderroot",
        "inbox",
        "sentitems",
        "drafts",
        "deleteditems",
        "outbox",
        "junkemail",
        "archive",
        "conversationhistory",
        "scheduled",
        "syncissues",
        "recipientcache",
        "calendar",
        "contacts",
        "tasks",
    }
)

EVENT_FIELDS_ALL = ("subject", "location", "time", "all_day", "body", "busy_status", "reminder", "categories", "sensitivity")
EVENT_FIELDS_BASIC = tuple(f for f in EVENT_FIELDS_ALL if f not in ("categories", "sensitivity"))
DEFAULT_WRITABLE_FIELDS = ("subject", "location", "start", "end", "all_day", "timezone")
WRITABLE_FIELD_URIS: dict[str, tuple[str, ...]] = {
    "subject": ("item:Subject",),
    "location": ("calendar:Location",),
    "start": ("calendar:Start",),
    "end": ("calendar:End",),
    "all_day": ("calendar:IsAllDayEvent",),
    "timezone": ("calendar:StartTimeZone", "calendar:EndTimeZone"),
    "body": ("item:Body",),
    "busy_status": ("calendar:LegacyFreeBusyStatus",),
    "reminder": ("item:ReminderIsSet", "item:ReminderMinutesBeforeStart"),
    "categories": ("item:Categories",),
    "sensitivity": ("item:Sensitivity",),
}
DEFAULT_PROTECTED_WRITABLE_URIS = tuple(u for n in DEFAULT_WRITABLE_FIELDS for u in WRITABLE_FIELD_URIS[n])

EAS_KEYS: dict[str, str] = {
    "enabled": "enabled",
    "versions": "versions",
    "window_max": "window_max",
    "heartbeat_min_seconds": "heartbeat_min_seconds",
    "heartbeat_max_seconds": "heartbeat_max_seconds",
    "wait_max_minutes": "wait_max_minutes",
    "device_limit": "device_limit",
    "ping_limit_per_user": "ping_limit_per_user",
    "device_expiry_days": "device_expiry_days",
    "request_max_seconds": "request_max_seconds",
    "request_body_max_bytes": "request_body_max_bytes",
    "response_max_bytes": "response_max_bytes",
    "body_max_chars": "body_max_chars",
    "delete_brake_minimum": "delete_brake_minimum",
}


class _Runtime:
    """Process-wide default, set once at startup from the configuration (`default_timezone`)."""

    default_timezone = "UTC"


class ConfigError(Exception):
    pass


class FeatureSet(IntEnum):
    """Event field set behind `features.event_fields`: BASIC lacks categories and sensitivity, DEFAULT has every event field."""

    BASIC = 0
    DEFAULT = 1


@dataclass(frozen=True, slots=True)
class CalendarConfig:
    uri: str
    key: str
    display_name: str
    default: bool
    write: Literal["caldav", "never"]
    owner: str


@dataclass(frozen=True, slots=True)
class Account:
    name: str
    address: str
    display_name: str
    calendar: tuple[CalendarConfig, ...]
    test: bool = False
    capture: bool = False
    eas: bool = False
    auto: bool = False

    def default_calendar(self) -> CalendarConfig:
        return next(k for k in self.calendar if k.default)

    def calendar_after_key(self, key: str) -> CalendarConfig | None:
        return next((k for k in self.calendar if k.key == key), None)


@dataclass(frozen=True, slots=True)
class DynamicConfig:
    """Rules for users without a `[[users.calendars]]` list: calendars are discovered after a successful login (see `discovery.py`)."""

    calendar_mode: Literal["explicit", "auto"] = "explicit"
    default_calendar: str = "only"
    include_shared: bool = False
    eas_enabled: bool = False


@dataclass(frozen=True, slots=True)
class EasConfig:
    enabled: bool = False
    acknowledged: bool = False
    versions: tuple[str, ...] = ("14.1",)
    window_max: int = 100
    heartbeat_min_seconds: int = 60
    heartbeat_max_seconds: int = 540
    wait_max_minutes: int = 9
    device_limit: int = 6
    ping_limit_per_user: int = 4
    device_expiry_days: int = 30
    request_max_seconds: int = 25
    request_body_max_bytes: int = 512 * 1024
    response_max_bytes: int = 768 * 1024
    body_max_chars: int = 32 * 1024
    delete_brake_minimum: int = 5


@dataclass(frozen=True, slots=True)
class Config:
    features: FeatureSet
    public_url: str
    caldav_base_url: str
    accounts: tuple[Account, ...]
    read_only_owners: frozenset[str] = frozenset()
    protected_property_prefixes: tuple[str, ...] = ()
    protected_writable_uris: frozenset[str] = frozenset(DEFAULT_PROTECTED_WRITABLE_URIS)
    event_fields: tuple[str, ...] = EVENT_FIELDS_ALL
    default_timezone: str = "UTC"
    autodiscover_culture: str = "en:us"
    listen: str = "0.0.0.0:8000"
    trusted_proxies: tuple[str, ...] = ()
    verify_tls: bool = True
    follow_redirects: bool = False
    connect_timeout_seconds: int = 5
    read_timeout_seconds: int = 30
    auth_mode: Literal["allowlist", "pattern"] = "allowlist"
    username_pattern: str = ""
    address_template: str = ""
    poll_active_seconds: int = 20
    poll_idle_seconds: int = 120
    ews_enabled: bool = True
    stream_max_seconds: int = 1800
    positive_cache_seconds: int = 300
    event_detail: Literal["folder", "item", "both"] = "both"
    failure_limit: int = 10
    initial_lock_seconds: int = 900
    commit: str = "unknown"
    state_dir: Path = field(default=Path(DEFAULT_STATE_DIR))
    logs_dir: Path | None = None
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_stdout: bool = True
    eas: EasConfig = field(default_factory=EasConfig)
    dynamic: DynamicConfig = field(default_factory=DynamicConfig)


def listen_address(listen: str) -> tuple[str, int]:
    """Splits `server.listen` (`host:port`, IPv6 hosts in brackets) into the host and port the server binds to."""
    host, sep, port = listen.rpartition(":")
    if not sep or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(listen)
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        if ":" not in host:
            raise ValueError(listen)
    elif ":" in host:
        raise ValueError(listen)
    if not host or not re.fullmatch(r"[A-Za-z0-9.:_-]+", host):
        raise ValueError(listen)
    return host, int(port)


def runtime_default_zone() -> str:
    """Default zone for floating ICS times and naive EWS times without a context zone; `UTC` without configuration."""
    return _Runtime.default_timezone


def set_runtime_default_zone(name: str) -> None:
    from zoneinfo import ZoneInfo

    ZoneInfo(name)
    _Runtime.default_timezone = name


def account_for(config: Config, user: str) -> Account | None:
    value = user.strip().lower()
    for account in config.accounts:
        if value in (account.name.lower(), account.address.lower()):
            return account
    return None


def dynamic_account(config: Config, login: str) -> Account | None:
    """The account for a login name that matches `auth.username_pattern` (pattern mode only). The address form of the login is accepted too.
    The name must also be a plain user name: nothing that could reach a CalDAV path (slashes, percent signs, URLs) ever passes."""
    if config.auth_mode != "pattern" or not config.username_pattern:
        return None
    value = login.strip()
    candidates = [value]
    if config.address_template:
        hit = re.fullmatch(re.escape(config.address_template).replace(re.escape("{user}"), "(.+)"), value, re.IGNORECASE)
        if hit:
            candidates.append(hit.group(1))
    for name in candidates:
        if USERNAME_PATTERN.fullmatch(name) and re.fullmatch(config.username_pattern, name):
            address = config.address_template.replace("{user}", name)
            return Account(name=name, address=address, display_name=name, calendar=(), eas=config.dynamic.eas_enabled, auto=True)
    return None


def renamed_dynamic_account(config: Config, account: Account, name: str) -> Account:
    """The pattern-mode account under the spelling of the user id the CalDAV server uses (state, ids and caches all hang on the name)."""
    return replace(account, name=name, address=config.address_template.replace("{user}", name), display_name=name)


# ------------------------------------------------------------------------------------------------ schema description

_SECTIONS: dict[tuple[str, ...], dict[str, str]] = {
    ("server",): {"public_url": "str", "listen": "str", "default_timezone": "str", "autodiscover_culture": "str", "trusted_proxies": "list"},
    ("caldav",): {"base_url": "str", "verify_tls": "bool", "follow_redirects": "bool", "connect_timeout_seconds": "int", "read_timeout_seconds": "int"},
    ("auth",): {"mode": "str", "allowed_users": "list", "username_pattern": "str", "address_template": "str", "positive_cache_seconds": "int", "failure_limit": "int", "initial_lock_seconds": "int"},
    ("protocols", "ews"): {"enabled": "bool", "stream_max_seconds": "int", "event_detail": "str"},
    ("protocols", "eas"): {
        "enabled": "bool",
        "acknowledge_mobile_cloud_processing": "bool",
        "versions": "list",
        "window_max": "int",
        "heartbeat_min_seconds": "int",
        "heartbeat_max_seconds": "int",
        "wait_max_minutes": "int",
        "device_limit": "int",
        "ping_limit_per_user": "int",
        "device_expiry_days": "int",
        "request_max_seconds": "int",
        "request_body_max_bytes": "int",
        "response_max_bytes": "int",
        "body_max_chars": "int",
        "delete_brake_minimum": "int",
    },
    ("features",): {"event_fields": "list", "read_only_owners": "list", "protected_property_prefixes": "list", "protected_event_writable_fields": "list"},
    ("dynamic_users",): {"calendar_mode": "str", "default_calendar": "str", "include_shared": "bool", "eas_enabled": "bool"},
}
_TOP_KEYS = {"config_version", "server", "caldav", "auth", "protocols", "features", "dynamic_users", "users"}
_USER_KEYS = {"username": "str", "address": "str", "display_name": "str", "eas_enabled": "bool", "calendar_mode": "str", "calendars": "tables"}
_CALENDAR_KEYS = {"path": "str", "id": "str", "display_name": "str", "default": "bool", "write": "str", "owner": "str"}

_SHORT_VARS: dict[str, tuple[str, ...]] = {
    "DAVBRIDGE_PUBLIC_URL": ("server", "public_url"),
    "DAVBRIDGE_CALDAV_URL": ("caldav", "base_url"),
    "DAVBRIDGE_TRUSTED_PROXIES": ("server", "trusted_proxies"),
    "DAVBRIDGE_ALLOWED_USERS": ("auth", "allowed_users"),
    "DAVBRIDGE_USER_PATTERN": ("auth", "username_pattern"),
    "DAVBRIDGE_ADDRESS_TEMPLATE": ("auth", "address_template"),
    "DAVBRIDGE_CALENDAR_MODE": ("dynamic_users", "calendar_mode"),
    "DAVBRIDGE_EAS_ENABLED": ("protocols", "eas", "enabled"),
    "DAVBRIDGE_ACKNOWLEDGE_MOBILE_CLOUD_PROCESSING": ("protocols", "eas", "acknowledge_mobile_cloud_processing"),
}
LOG_LEVELS = ("debug", "info", "warning", "error")


def _error(text: str) -> ConfigError:
    return ConfigError(text)


def _points(table_path: tuple[str, ...]) -> str:
    return ".".join(table_path)


def _parse_env_value(kind: str, value: str, name: str) -> Any:
    if kind == "bool":
        if value not in ("true", "false"):
            raise _error(f"environment variable {name} must be exactly 'true' or 'false'")
        return value == "true"
    if kind == "int":
        if not re.fullmatch(r"-?[0-9]{1,12}", value):
            raise _error(f"environment variable {name} must be an integer")
        return int(value)
    if kind == "list":
        try:
            raw = json.loads(value)
        except ValueError:
            raise _error(f'environment variable {name} must be a JSON list (for example ["a","b"])') from None
        if not isinstance(raw, list):
            raise _error(f"environment variable {name} must be a JSON list")
        return raw
    return value


def _set_nested(raw: dict, table_path: tuple[str, ...], value: Any) -> None:
    node = raw
    for part in table_path[:-1]:
        nxt = node.get(part)
        if nxt is None:
            nxt = node[part] = {}
        if not isinstance(nxt, dict):
            raise _error(f"{part} must be a table")
        node = nxt
    node[table_path[-1]] = value


def apply_environment(raw: dict, environment: Mapping[str, str]) -> None:
    """Applies the environment overrides in the documented order: general `DAVBRIDGE__SECTION__KEY`, then the named short variables."""
    kinds = {section + (key,): kind for section, key_kind in _SECTIONS.items() for key, kind in key_kind.items()}
    for name in sorted(environment):
        if not name.startswith("DAVBRIDGE__"):
            continue
        table_path = tuple(part.lower() for part in name[len("DAVBRIDGE__") :].split("__"))
        kind = kinds.get(table_path)
        if kind is None or any(not part for part in table_path):
            raise _error(f"unknown environment variable {name}")
        _set_nested(raw, table_path, _parse_env_value(kind, environment[name], name))
    for name, table_path in _SHORT_VARS.items():
        if name in environment:
            _set_nested(raw, table_path, _parse_env_value(kinds[table_path], environment[name], name))


# ------------------------------------------------------------------------------------------------ typed readers


def _table(raw: Mapping, table_path: tuple[str, ...], known: Mapping[str, str] | set[str]) -> dict:
    if not isinstance(raw, Mapping):
        raise _error(f"{_points(table_path)} must be a table")
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise _error(f"unknown key {_points(table_path + (unknown[0],))}")
    return dict(raw)


def _bool(tab: Mapping, table_path: tuple[str, ...], key: str, default: bool) -> bool:
    value = tab.get(key, default)
    if not isinstance(value, bool):
        raise _error(f"{_points(table_path + (key,))} must be true or false")
    return value


def _int(tab: Mapping, table_path: tuple[str, ...], key: str, default: int, low: int, high: int) -> int:
    value = tab.get(key, default)
    if type(value) is not int:
        raise _error(f"{_points(table_path + (key,))} must be an integer")
    if not low <= value <= high:
        raise _error(f"{_points(table_path + (key,))} must be between {low} and {high}")
    return value


def _str(tab: Mapping, table_path: tuple[str, ...], key: str, default: str | None = None) -> str:
    value = tab.get(key, default)
    if value is None:
        raise _error(f"{_points(table_path + (key,))} is required")
    if not isinstance(value, str):
        raise _error(f"{_points(table_path + (key,))} must be a string")
    return value


def _enum(tab: Mapping, table_path: tuple[str, ...], key: str, default: str, allowed: tuple[str, ...]) -> str:
    value = _str(tab, table_path, key, default)
    if value not in allowed:
        raise _error(f"{_points(table_path + (key,))} must be one of: {', '.join(allowed)}")
    return value


def _strings(tab: Mapping, table_path: tuple[str, ...], key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = tab.get(key, list(default))
    if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
        raise _error(f"{_points(table_path + (key,))} must be a list of strings")
    return tuple(value)


def _https_url(value: str, name: str, *, path_allowed: bool, unsafe: bool) -> str:
    parts = urlsplit(value)
    allowed = ("https", "http") if unsafe else ("https",)
    if parts.scheme not in allowed:
        raise _error(f"{name} must be an https URL" + (" (plain http is only accepted with " + ALLOW_INSECURE_HTTP_VAR + "=true for development)" if parts.scheme == "http" else ""))
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise _error(f"{name} must be a plain URL without credentials, query or fragment")
    if not path_allowed and parts.path not in ("", "/"):
        raise _error(f"{name} must not contain a path")
    try:
        parts.port
    except ValueError:
        raise _error(f"{name} has an invalid port") from None
    if any(c in value for c in " \t\r\n\\"):
        raise _error(f"{name} contains invalid characters")
    return value.rstrip("/")


# ------------------------------------------------------------------------------------------------ sections


def _server(raw: Mapping, unsafe: bool) -> dict:
    table_path = ("server",)
    tab = _table(raw.get("server", {}), table_path, _SECTIONS[table_path])
    url = _https_url(_str(tab, table_path, "public_url"), "server.public_url", path_allowed=False, unsafe=unsafe)
    listen = _str(tab, table_path, "listen", "0.0.0.0:8000")
    try:
        listen_address(listen)
    except ValueError:
        raise _error("server.listen must have the form host:port (IPv6 hosts in brackets, for example [::]:8000)") from None
    zone = _str(tab, table_path, "default_timezone", "UTC")
    _check_zone(zone)
    culture = _str(tab, table_path, "autodiscover_culture", "en:us")
    if not CULTURE_PATTERN.match(culture):
        raise _error("server.autodiscover_culture must be language:country in lower case, for example en:us")
    proxies = []
    for entry in _strings(tab, table_path, "trusted_proxies"):
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise _error(f"server.trusted_proxies: {entry!r} is not a valid address or CIDR") from None
        if network.prefixlen == 0:
            raise _error(
                f"server.trusted_proxies: {entry!r} would trust every address: any client could then forge its own address and evade or trigger lock-outs; list the proxy itself or its narrow subnet"
            )
        proxies.append(str(network))
    return {"public_url": url, "listen": listen, "zone": zone, "culture": culture, "proxies": tuple(dict.fromkeys(proxies))}


def _check_zone(raw: str) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not raw:
        raise _error("server.default_timezone must be an IANA time zone name")
    try:
        ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise _error(f"server.default_timezone: unknown time zone {raw!r}") from None


def _caldav(raw: Mapping, unsafe: bool) -> dict:
    table_path = ("caldav",)
    tab = _table(raw.get("caldav", {}), table_path, _SECTIONS[table_path])
    return {
        "base": _https_url(_str(tab, table_path, "base_url"), "caldav.base_url", path_allowed=True, unsafe=unsafe),
        "verify_tls": _bool(tab, table_path, "verify_tls", True),
        "redirects": _bool(tab, table_path, "follow_redirects", False),
        "connect": _int(tab, table_path, "connect_timeout_seconds", 5, 1, 60),
        "read": _int(tab, table_path, "read_timeout_seconds", 30, 1, 600),
    }


def _auth(raw: Mapping) -> dict:
    table_path = ("auth",)
    tab = _table(raw.get("auth", {}), table_path, _SECTIONS[table_path])
    mode = _enum(tab, table_path, "mode", "allowlist", ("allowlist", "pattern"))
    allowed = _strings(tab, table_path, "allowed_users")
    for name in allowed:
        if not USERNAME_PATTERN.match(name):
            raise _error(f"auth.allowed_users: {name!r} is not a valid user name")
    pattern = _str(tab, table_path, "username_pattern", "")
    if pattern:
        try:
            re.compile(pattern)
        except re.error:
            raise _error("auth.username_pattern is not a valid regular expression") from None
    template = _str(tab, table_path, "address_template", "")
    if template and (template.count("{user}") != 1 or "{" in template.replace("{user}", "") or "}" in template.replace("{user}", "")):
        raise _error("auth.address_template must contain {user} exactly once and no other placeholder")
    if mode == "pattern":
        if not pattern:
            raise _error('auth.username_pattern is required with auth.mode = "pattern"')
        if not (pattern.startswith("^") and pattern.endswith("$")):
            raise _error("auth.username_pattern must be fully anchored (start with ^ and end with $)")
    return {
        "mode": mode,
        "allowed": allowed,
        "pattern": pattern,
        "template": template,
        "cache": _int(tab, table_path, "positive_cache_seconds", 300, 0, 3600),
        "limit": _int(tab, table_path, "failure_limit", 10, 1, 1000),
        "lock": _int(tab, table_path, "initial_lock_seconds", 900, 1, 86400),
    }


def _ews(raw: Mapping) -> dict:
    table_path = ("protocols", "ews")
    tab = _table(raw.get("protocols", {}).get("ews", {}) if isinstance(raw.get("protocols", {}), Mapping) else {}, table_path, _SECTIONS[table_path])
    return {
        "enabled": _bool(tab, table_path, "enabled", True),
        "stream": _int(tab, table_path, "stream_max_seconds", 1800, 60, 1800),
        "detail": _enum(tab, table_path, "event_detail", "both", ("folder", "item", "both")),
    }


def _eas(raw: Mapping, count_eas_user: int) -> EasConfig:
    table_path = ("protocols", "eas")
    tab = _table(raw.get("protocols", {}).get("eas", {}) if isinstance(raw.get("protocols", {}), Mapping) else {}, table_path, _SECTIONS[table_path])
    default = EasConfig()
    values: dict[str, Any] = {}
    enabled = _bool(tab, table_path, "enabled", False)
    acknowledged = _bool(tab, table_path, "acknowledge_mobile_cloud_processing", False)
    versions = _strings(tab, table_path, "versions", default.versions)
    if versions != ("14.1",):
        raise _error("protocols.eas.versions: only 14.1 is supported")
    for english, internal in EAS_KEYS.items():
        if english in ("enabled", "versions"):
            continue
        values[internal] = tab.get(english, getattr(default, internal))
        if type(values[internal]) is not int:
            raise _error(f"{_points(table_path + (english,))} must be an integer")
    eas = EasConfig(enabled=enabled, acknowledged=acknowledged, versions=versions, **values)
    areas = (
        ("window_max", eas.window_max, 1, 512),
        ("wait_max_minutes", eas.wait_max_minutes, 1, 9),
        ("device_limit", eas.device_limit, 1, 20),
        ("ping_limit_per_user", eas.ping_limit_per_user, 1, 8),
        ("device_expiry_days", eas.device_expiry_days, 1, 365),
        ("delete_brake_minimum", eas.delete_brake_minimum, 1, 1000),
        ("request_max_seconds", eas.request_max_seconds, 5, 60),
        ("request_body_max_bytes", eas.request_body_max_bytes, 1024, 1024 * 1024),
        ("response_max_bytes", eas.response_max_bytes, 64 * 1024, 4 * 1024 * 1024),
        ("body_max_chars", eas.body_max_chars, 1024, 256 * 1024),
    )
    for name, value, low, high in areas:
        if not low <= value <= high:
            raise _error(f"protocols.eas.{name} must be between {low} and {high}")
    if not 60 <= eas.heartbeat_min_seconds <= eas.heartbeat_max_seconds <= 540:
        raise _error("protocols.eas: 60 <= heartbeat_min_seconds <= heartbeat_max_seconds <= 540 is required")
    if enabled and count_eas_user == 0:
        raise _error("protocols.eas.enabled = true but no user has eas_enabled = true")
    if count_eas_user and not enabled:
        raise _error("a user has eas_enabled = true but protocols.eas.enabled is false")
    if enabled and not acknowledged:
        raise _error(
            "protocols.eas.enabled = true requires protocols.eas.acknowledge_mobile_cloud_processing = true "
            "(Outlook mobile processes a copy of the calendar in Microsoft's cloud; read docs/privacy.md)"
        )
    return eas


def _features(raw: Mapping) -> dict:
    table_path = ("features",)
    tab = _table(raw.get("features", {}), table_path, _SECTIONS[table_path])
    fields = _strings(tab, table_path, "event_fields", EVENT_FIELDS_ALL)
    unknown = [f for f in fields if f not in EVENT_FIELDS_ALL]
    if unknown:
        raise _error(f"features.event_fields: unknown field {unknown[0]!r}; known fields: {', '.join(EVENT_FIELDS_ALL)}")
    if len(set(fields)) != len(fields):
        raise _error("features.event_fields contains duplicates")
    if set(fields) == set(EVENT_FIELDS_ALL):
        feature_set = FeatureSet.DEFAULT
    elif set(fields) == set(EVENT_FIELDS_BASIC):
        feature_set = FeatureSet.BASIC
    else:
        raise _error("features.event_fields: this version supports exactly the full default set or the default set without categories and sensitivity")
    read_only = _strings(tab, table_path, "read_only_owners")
    prefixes = _strings(tab, table_path, "protected_property_prefixes")
    for p in prefixes:
        if not PREFIX_PATTERN.match(p):
            raise _error("features.protected_property_prefixes: complete upper-case prefixes such as X-EXAMPLE- are required")
    writable = _strings(tab, table_path, "protected_event_writable_fields", DEFAULT_WRITABLE_FIELDS)
    for name in writable:
        if name not in WRITABLE_FIELD_URIS:
            raise _error(f"features.protected_event_writable_fields: unknown field {name!r}; known fields: {', '.join(WRITABLE_FIELD_URIS)}")
    return {
        "feature_set": feature_set,
        "fields": tuple(fields),
        "read_only": frozenset(read_only),
        "prefixes": tuple(dict.fromkeys(prefixes)),
        "writable": frozenset(u for n in writable for u in WRITABLE_FIELD_URIS[n]),
    }


def _dynamic(raw: Mapping) -> DynamicConfig:
    table_path = ("dynamic_users",)
    tab = _table(raw.get("dynamic_users", {}), table_path, _SECTIONS[table_path])
    default = _str(tab, table_path, "default_calendar", "only")
    if default not in ("only", "first-writable") and not valid_calendar_segment(default):
        raise _error('dynamic_users.default_calendar must be "only", "first-writable" or the exact calendar path (one path segment, for example "personal")')
    return DynamicConfig(
        calendar_mode=_enum(tab, table_path, "calendar_mode", "explicit", ("explicit", "auto")),  # type: ignore[arg-type]
        default_calendar=default,
        include_shared=_bool(tab, table_path, "include_shared", False),
        eas_enabled=_bool(tab, table_path, "eas_enabled", False),
    )


def _calendar(raw: Any, table_path: tuple[str, ...], user: str, read_only: frozenset[str]) -> CalendarConfig:
    tab = _table(raw, table_path, _CALENDAR_KEYS)
    path = _str(tab, table_path, "path")
    identifier = _str(tab, table_path, "id")
    write = _enum(tab, table_path, "write", "caldav", ("caldav", "never"))
    cal = CalendarConfig(
        uri=path,
        key=identifier,
        display_name=_str(tab, table_path, "display_name", identifier),
        default=_bool(tab, table_path, "default", False),
        write=write,
        owner=_str(tab, table_path, "owner", user),  # type: ignore[arg-type]
    )
    if not KEY_PATTERN.match(cal.key) or cal.key in RESERVED:
        raise _error(f"{_points(table_path)}.id must match [a-z0-9-]{{1,40}} and must not be a reserved folder name")
    if not valid_calendar_segment(cal.uri):
        raise _error(f"{_points(table_path)}.path contains invalid characters (a single path segment; no encoded slash, backslash or dot)")
    if cal.owner in read_only and cal.write != "never":
        raise _error(f'{_points(table_path)}: the owner {cal.owner!r} is listed in features.read_only_owners, set write = "never"')
    return cal


def _user(raw: Any, index: int, template: str, read_only: frozenset[str]) -> Account:
    table_path = (f"users[{index}]",)
    tab = _table(raw, table_path, _USER_KEYS)
    name = _str(tab, table_path, "username")
    if not USERNAME_PATTERN.match(name):
        raise _error(f"{_points(table_path)}.username {name!r} is not a valid user name")
    mode = _enum(tab, table_path, "calendar_mode", "explicit", ("explicit", "auto"))
    address = _str(tab, table_path, "address", template.replace("{user}", name) if template else None)
    if not ADDRESS_PATTERN.match(address):
        raise _error(f"{_points(table_path)}.address is not a valid e-mail style address")
    calendar_raw = tab.get("calendars")
    if mode == "auto":
        if calendar_raw:
            raise _error(f'{_points(table_path)}: calendar_mode = "auto" discovers the calendars; remove the [[users.calendars]] entries or use "explicit"')
        return Account(name=name, address=address, display_name=_str(tab, table_path, "display_name", name), calendar=(), eas=_bool(tab, table_path, "eas_enabled", False), auto=True)
    if not isinstance(calendar_raw, list) or not calendar_raw:
        raise _error(f"{_points(table_path)} needs at least one [[users.calendars]] entry")
    calendar = tuple(_calendar(k, (f"users[{index}]", f"calendars[{i}]"), name, read_only) for i, k in enumerate(calendar_raw))
    if sum(k.default for k in calendar) != 1:
        raise _error(f"user {name!r}: exactly one calendar must have default = true")
    for attribute, label in (("key", "id"), ("uri", "path")):
        values = [getattr(k, attribute) for k in calendar]
        if len(set(values)) != len(values):
            raise _error(f"user {name!r}: calendar {label} values must be unique")
    return Account(name=name, address=address, display_name=_str(tab, table_path, "display_name", name), calendar=calendar, eas=_bool(tab, table_path, "eas_enabled", False))


# ------------------------------------------------------------------------------------------------ assembly


def from_dict(raw: Mapping, environment: Mapping[str, str] | None = None) -> Config:
    """Validates an already parsed configuration (TOML structure, environment overrides NOT applied here)."""
    environment = environment or {}
    raw = dict(raw)
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise _error(f"unknown key {unknown[0]}")
    version = raw.get("config_version")
    if type(version) is not int:
        raise _error("config_version is required and must be the integer 1")
    if version != CONFIG_VERSION:
        raise _error(f"unsupported config_version {version}; this version of DAVbridge reads config_version = {CONFIG_VERSION}")
    protocols = raw.get("protocols", {})
    if not isinstance(protocols, Mapping):
        raise _error("protocols must be a table")
    unknown = sorted(set(protocols) - {"ews", "eas"})
    if unknown:
        raise _error(f"unknown key protocols.{unknown[0]}")
    unsafe = environment.get(ALLOW_INSECURE_HTTP_VAR, "false") == "true"
    if environment.get(ALLOW_INSECURE_HTTP_VAR, "false") not in ("true", "false"):
        raise _error(f"environment variable {ALLOW_INSECURE_HTTP_VAR} must be exactly 'true' or 'false'")

    server = _server(raw, unsafe)
    caldav = _caldav(raw, unsafe)
    auth = _auth(raw)
    features = _features(raw)
    ews = _ews(raw)
    dynamic = _dynamic(raw)
    template = auth["template"] or "{user}@" + urlsplit(server["public_url"]).hostname

    user_raw = raw.get("users", [])
    if not isinstance(user_raw, list):
        raise _error("users must be an array of tables ([[users]])")
    accounts = tuple(_user(b, i, template, features["read_only"]) for i, b in enumerate(user_raw))
    names = [k.name.lower() for k in accounts]
    if len(set(names)) != len(names):
        raise _error("duplicate user name in [[users]] (names are compared case-insensitively)")
    addresses = [k.address.lower() for k in accounts]
    if len(set(addresses)) != len(addresses) or set(addresses) & set(names):
        raise _error("user addresses must be unique and must not equal another user's name")
    allowed = [n.lower() for n in auth["allowed"]]
    if len(set(allowed)) != len(allowed):
        raise _error("duplicate entry in auth.allowed_users")
    if auth["mode"] == "allowlist":
        without_block = [n for n in auth["allowed"] if n.lower() not in names]
        if without_block and dynamic.calendar_mode != "auto":
            raise _error(f'auth.allowed_users: {without_block[0]!r} has no [[users]] block; add one or set [dynamic_users] calendar_mode = "auto" to discover its calendars')
        for name in without_block:
            address = template.replace("{user}", name)
            if not ADDRESS_PATTERN.match(address) or address.lower() in names + addresses:
                raise _error(f"auth.allowed_users: the address derived for {name!r} from auth.address_template is invalid or already in use")
            accounts += (Account(name=name, address=address, display_name=name, calendar=(), eas=dynamic.eas_enabled, auto=True),)
            names.append(name.lower())
            addresses.append(address.lower())
        if not accounts:
            raise _error("no user configured: add at least one [[users]] entry")
    else:
        if dynamic.calendar_mode != "auto":
            raise _error('auth.mode = "pattern" requires [dynamic_users] calendar_mode = "auto": calendars cannot be configured per dynamic user')
        if not template:
            raise _error('auth.mode = "pattern" requires an address_template or a public_url host')
    eas = _eas(raw, sum(k.eas for k in accounts) + (1 if auth["mode"] == "pattern" and dynamic.eas_enabled else 0))
    if not ews["enabled"] and not eas.enabled:
        raise _error("at least one of protocols.ews.enabled and protocols.eas.enabled must be true")
    state = environment.get("DAVBRIDGE_STATE_DIR", DEFAULT_STATE_DIR)
    logs = environment.get("DAVBRIDGE_LOGS")
    level_name = environment.get("DAVBRIDGE_LOG_LEVEL", "info").lower()
    if level_name not in LOG_LEVELS:
        raise _error(f"DAVBRIDGE_LOG_LEVEL must be one of: {', '.join(LOG_LEVELS)}")
    return Config(
        features=features["feature_set"],
        public_url=server["public_url"],
        caldav_base_url=caldav["base"],
        accounts=accounts,
        read_only_owners=features["read_only"],
        protected_property_prefixes=features["prefixes"],
        protected_writable_uris=features["writable"],
        event_fields=features["fields"],
        default_timezone=server["zone"],
        autodiscover_culture=server["culture"],
        listen=server["listen"],
        trusted_proxies=server["proxies"],
        verify_tls=caldav["verify_tls"],
        follow_redirects=caldav["redirects"],
        connect_timeout_seconds=caldav["connect"],
        read_timeout_seconds=caldav["read"],
        auth_mode=auth["mode"],
        username_pattern=auth["pattern"],
        address_template=template,
        ews_enabled=ews["enabled"],
        stream_max_seconds=ews["stream"],
        event_detail=ews["detail"],
        positive_cache_seconds=auth["cache"],
        failure_limit=auth["limit"],
        initial_lock_seconds=auth["lock"],
        commit=environment.get("GIT_COMMIT", "unknown"),
        state_dir=Path(state),
        logs_dir=Path(logs) if logs else None,
        log_level=level_name,  # type: ignore[arg-type]
        eas=eas,
        dynamic=dynamic,
    )


def load_raw(table_path: Path) -> dict:
    try:
        with open(table_path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise _error(f"configuration file not found: {table_path}") from None
    except PermissionError:
        raise _error(f"configuration file is not readable: {table_path}") from None
    except tomllib.TOMLDecodeError as e:
        raise _error(f"invalid TOML in {table_path}: {e}") from None
    except IsADirectoryError:
        raise _error(f"configuration path is a directory: {table_path}") from None


def load(table_path: Path, environment: Mapping[str, str]) -> Config:
    """TOML file + environment overrides + strict validation."""
    raw = load_raw(table_path)
    apply_environment(raw, environment)
    return from_dict(raw, environment)


def config_path(environment: Mapping[str, str]) -> Path:
    return Path(environment.get("DAVBRIDGE_CONFIG", DEFAULT_CONFIG_PATH))


def check_start(config: Config) -> None:
    """Refusals that are valid configuration but must not run. Currently there are none; the hook stays so that `serve` and `check` share one gate."""


def public_representation(config: Config) -> dict:
    """Effective configuration in the public TOML layout (no secrets exist in the configuration)."""
    reversed = {v: k for k, v in EAS_KEYS.items()}
    eas = {"enabled": config.eas.enabled, "acknowledge_mobile_cloud_processing": config.eas.acknowledged, "versions": list(config.eas.versions)}
    for internal, english in reversed.items():
        if english not in ("enabled", "versions"):
            eas[english] = getattr(config.eas, internal)
    fields_back = {u: n for n in WRITABLE_FIELD_URIS for u in WRITABLE_FIELD_URIS[n][:1]}
    return {
        "config_version": CONFIG_VERSION,
        "server": {
            "public_url": config.public_url,
            "listen": config.listen,
            "default_timezone": config.default_timezone,
            "autodiscover_culture": config.autodiscover_culture,
            "trusted_proxies": list(config.trusted_proxies),
        },
        "caldav": {
            "base_url": config.caldav_base_url,
            "verify_tls": config.verify_tls,
            "follow_redirects": config.follow_redirects,
            "connect_timeout_seconds": config.connect_timeout_seconds,
            "read_timeout_seconds": config.read_timeout_seconds,
        },
        "auth": {
            "mode": config.auth_mode,
            "allowed_users": [k.name for k in config.accounts],
            "username_pattern": config.username_pattern,
            "address_template": config.address_template,
            "positive_cache_seconds": config.positive_cache_seconds,
            "failure_limit": config.failure_limit,
            "initial_lock_seconds": config.initial_lock_seconds,
        },
        "protocols": {"ews": {"enabled": config.ews_enabled, "stream_max_seconds": config.stream_max_seconds, "event_detail": config.event_detail}, "eas": eas},
        "features": {
            "event_fields": list(config.event_fields),
            "read_only_owners": sorted(config.read_only_owners),
            "protected_property_prefixes": list(config.protected_property_prefixes),
            "protected_event_writable_fields": [
                n for n in WRITABLE_FIELD_URIS if all(u in config.protected_writable_uris for u in WRITABLE_FIELD_URIS[n]) and fields_back.get(WRITABLE_FIELD_URIS[n][0]) == n
            ],
        },
        "dynamic_users": {
            "calendar_mode": config.dynamic.calendar_mode,
            "default_calendar": config.dynamic.default_calendar,
            "include_shared": config.dynamic.include_shared,
            "eas_enabled": config.dynamic.eas_enabled,
        },
        "users": [
            {
                "username": k.name,
                "address": k.address,
                "display_name": k.display_name,
                "eas_enabled": k.eas,
                "calendar_mode": "auto" if k.auto else "explicit",
                **({} if k.auto else {"calendars": [{"path": c.uri, "id": c.key, "display_name": c.display_name, "default": c.default, "write": c.write, "owner": c.owner} for c in k.calendar]}),
            }
            for k in config.accounts
        ],
    }


def read_key(table_path: Path) -> bytes:
    """hmac.key: 32 bytes raw or Base64 of them. The file must be a regular file (no symbolic link) that belongs to the effective user
    and has no group or other permission bits: whoever can read the key can forge every identifier DAVbridge hands out."""
    try:
        fd = os.open(table_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise ConfigError(f"hmac.key not found at {table_path}; create it once with `davbridge init`") from None
    except PermissionError:
        raise ConfigError(f"hmac.key at {table_path} is not readable") from None
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ConfigError(f"hmac.key at {table_path} is a symbolic link; it must be a regular file inside the state directory") from None
        raise ConfigError(f"hmac.key at {table_path} cannot be opened: {e.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"hmac.key at {table_path} is not a regular file")
        if info.st_uid != os.geteuid():
            raise ConfigError(f"hmac.key at {table_path} is owned by uid {info.st_uid}, not by the user running DAVbridge (uid {os.geteuid()}); fix it with `chown`")
        if info.st_mode & 0o077:
            raise ConfigError(f"hmac.key at {table_path} is accessible to group or others (mode {info.st_mode & 0o777:04o}); fix it with `chmod 600`")
        raw = os.read(fd, 4096).strip()
    finally:
        os.close(fd)
    try:
        decrypted = base64.b64decode(raw, validate=True)
        if len(decrypted) == 32:
            return decrypted
    except ValueError:
        pass
    if len(raw) == 32:
        return raw
    raise ConfigError("hmac.key must contain 32 bytes (raw or Base64)")
