"""Offline converter from the legacy (German-key) configuration layout to the current `config_version = 1` layout."""

from __future__ import annotations

import copy
import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import toml_writer
from .config import EVENT_FIELDS_ALL, EVENT_FIELDS_BASIC, WRITABLE_FIELD_URIS, ConfigError, from_dict

MAX_SOURCE_BYTES = 1024 * 1024
FEATURE_PROFILES = {"default": EVENT_FIELDS_ALL, "basic": EVENT_FIELDS_BASIC}
LEGACY_STREAM_DEFAULT = 540

_TOP = {
    "stufe",
    "basis_url",
    "caldav_basis",
    "default_timezone",
    "autodiscover_culture",
    "richtlinie",
    "konto",
    "eas",
    "poll_aktiv_s",
    "poll_ruhe_s",
    "stream_max_s",
    "zugang_pruef_s",
    "ereignis_detail",
    "delete_ohne_changekey",
    "messmodus_tolerant",
    "sperre_fehlversuche",
    "sperre_sekunden",
}
_POLICY = {"nur_lesen_eigentuemer", "protected_property_prefixes", "protected_event_writable_fields"}
_ACCOUNT = {"name", "adresse", "anzeigename", "kalender", "eas", "test", "messung"}
_CALENDAR = {"uri", "schluessel", "anzeigename", "standard", "schreiben", "eigentuemer"}
_DETAIL = {"ordner": "folder", "element": "item", "beide": "both"}
_WRITE = {"nextcloud": "caldav", "nie": "never"}
_EAS_LEGACY = {
    "aktiv": "enabled",
    "versionen": "versions",
    "fenster_max": "window_max",
    "heartbeat_min_s": "heartbeat_min_seconds",
    "heartbeat_max_s": "heartbeat_max_seconds",
    "wait_max_min": "wait_max_minutes",
    "max_geraete": "device_limit",
    "max_pings_je_konto": "ping_limit_per_user",
    "geraet_ablauf_tage": "device_expiry_days",
    "anfrage_max_s": "request_max_seconds",
    "koerper_max": "request_body_max_bytes",
    "antwort_max": "response_max_bytes",
    "body_max_zeichen": "body_max_chars",
    "loeschbremse_min": "delete_brake_minimum",
}
_FIELD_NAMES = {uris[0]: name for name, uris in WRITABLE_FIELD_URIS.items()}


class MigrationError(Exception):
    pass


@dataclass(slots=True)
class MigrationResult:
    document: dict[str, Any]
    obsolete: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


def _check_keys(table: Any, allowed: set[str], where: str) -> dict:
    if not isinstance(table, Mapping):
        raise MigrationError(f"{where} must be a table")
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise MigrationError(f"unknown legacy key {where}{unknown[0]!r}; the converter does not guess - remove or rename it first")
    return dict(table)


def _writable_fields(uris: Any, result: MigrationResult) -> list[str]:
    if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
        raise MigrationError("richtlinie.protected_event_writable_fields must be a list of strings")
    names: list[str] = []
    remaining = set(uris)
    for name, group in WRITABLE_FIELD_URIS.items():
        if remaining >= set(group):
            names.append(name)
            remaining -= set(group)
    if remaining:
        result.notes.append("protected_event_writable_fields: entries without an equivalent were dropped: " + ", ".join(sorted(remaining)))
    return names


def migrate_legacy(old: Mapping[str, Any], *, feature_profile: str | None = None) -> MigrationResult:
    """Pure conversion. Raises MigrationError for unknown keys and for stages other than M2 without an explicit feature profile."""
    old = _check_keys(old, _TOP, "")
    result = MigrationResult(document={})
    stage = old.get("stufe")
    if not isinstance(stage, str) or stage not in ("M0", "M1", "M2", "M3"):
        raise MigrationError("legacy key 'stufe' is missing or unknown (expected M0, M1, M2 or M3)")
    if feature_profile is not None and feature_profile not in FEATURE_PROFILES:
        raise MigrationError(f"unknown feature profile {feature_profile!r}; choose one of: {', '.join(FEATURE_PROFILES)}")
    if stage != "M2" and feature_profile is None:
        raise MigrationError(
            f"the legacy rollout stage {stage} has no equivalent in the current configuration (only the former stage M2 is the default). "
            f"Check which fields you want and re-run with --feature-profile default (all event fields, the former M2 set) or "
            f"--feature-profile basic (without categories and sensitivity)"
        )
    profile = feature_profile or "default"
    result.obsolete.append(f"stufe = {stage}")
    result.notes.append(f"feature profile: {profile} ({'selected explicitly' if feature_profile else 'equivalent to the legacy stage M2'})")

    for key in ("messmodus_tolerant", "delete_ohne_changekey"):
        if key in old:
            result.obsolete.append(f"{key} (obsolete and unsafe; removed)")
    for key in ("poll_aktiv_s", "poll_ruhe_s"):
        if key in old:
            result.obsolete.append(f"{key} (no longer configurable; fixed internal value)")

    policy = _check_keys(old.get("richtlinie", {}), _POLICY, "richtlinie.")
    server: dict[str, Any] = {}
    for legacy in ("basis_url", "caldav_basis"):
        if legacy not in old:
            raise MigrationError(f"legacy key {legacy!r} is missing")
    server["public_url"] = old["basis_url"]
    if "default_timezone" in old:
        server["default_timezone"] = old["default_timezone"]
    if "autodiscover_culture" in old:
        server["autodiscover_culture"] = old["autodiscover_culture"]
    server["trusted_proxies"] = []
    result.actions.append("server.trusted_proxies is empty (trust nobody). Set it to the address of your reverse proxy, otherwise all clients share one lock-out counter (docs/reverse-proxy.md)")

    caldav = {"base_url": old["caldav_basis"]}
    auth: dict[str, Any] = {"mode": "allowlist"}
    for legacy, current in (("zugang_pruef_s", "positive_cache_seconds"), ("sperre_fehlversuche", "failure_limit"), ("sperre_sekunden", "initial_lock_seconds")):
        if legacy in old:
            auth[current] = old[legacy]

    ews: dict[str, Any] = {"enabled": True}
    ews["stream_max_seconds"] = old.get("stream_max_s", LEGACY_STREAM_DEFAULT)
    if "stream_max_s" not in old:
        result.notes.append(f"protocols.ews.stream_max_seconds = {LEGACY_STREAM_DEFAULT} keeps the legacy default (the new default is 1800)")
    if "ereignis_detail" in old:
        if old["ereignis_detail"] not in _DETAIL:
            raise MigrationError("legacy key 'ereignis_detail' must be ordner, element or beide")
        ews["event_detail"] = _DETAIL[old["ereignis_detail"]]

    eas_old = _check_keys(old.get("eas", {}), set(_EAS_LEGACY), "eas.")
    eas: dict[str, Any] = {}
    for legacy, value in eas_old.items():
        eas[_EAS_LEGACY[legacy]] = value
    if "enabled" in eas:
        eas = {"enabled": eas["enabled"], "acknowledge_mobile_cloud_processing": False, **{k: v for k, v in eas.items() if k != "enabled"}}
    if eas.get("enabled") is True:
        result.actions.append(
            "EAS is enabled but acknowledge_mobile_cloud_processing is false (the converter never sets it): read docs/privacy.md "
            "and set protocols.eas.acknowledge_mobile_cloud_processing = true yourself, otherwise the service refuses to start"
        )

    features: dict[str, Any] = {
        "event_fields": list(FEATURE_PROFILES[profile]),
        "read_only_owners": list(policy.get("nur_lesen_eigentuemer", [])),
        "protected_property_prefixes": list(policy.get("protected_property_prefixes", [])),
    }
    if "protected_event_writable_fields" in policy:
        features["protected_event_writable_fields"] = _writable_fields(policy["protected_event_writable_fields"], result)

    users: list[dict[str, Any]] = []
    accounts = old.get("konto", [])
    if not isinstance(accounts, list) or not accounts:
        raise MigrationError("no [[konto]] entry found")
    for index, raw in enumerate(accounts):
        account = _check_keys(raw, _ACCOUNT, f"konto[{index}].")
        for flag in ("test", "messung"):
            if flag in account:
                result.obsolete.append(f"konto {account.get('name', index)!r}: {flag} (no longer exists)")
        calendars = []
        for cal_index, cal_raw in enumerate(account.get("kalender", [])):
            cal = _check_keys(cal_raw, _CALENDAR, f"konto[{index}].kalender[{cal_index}].")
            if cal.get("schreiben") not in _WRITE:
                raise MigrationError(f"konto[{index}].kalender[{cal_index}].schreiben must be nextcloud or nie")
            calendars.append(
                {
                    "path": cal.get("uri"),
                    "id": cal.get("schluessel"),
                    "display_name": cal.get("anzeigename"),
                    "default": bool(cal.get("standard", False)),
                    "write": _WRITE[cal["schreiben"]],
                    "owner": cal.get("eigentuemer"),
                }
            )
        users.append(
            {
                "username": account.get("name"),
                "address": account.get("adresse"),
                "display_name": account.get("anzeigename"),
                "eas_enabled": bool(account.get("eas", False)),
                "calendar_mode": "explicit",
                "calendars": calendars,
            }
        )
    auth["allowed_users"] = [u["username"] for u in users if isinstance(u["username"], str)]

    result.document = {
        "config_version": 1,
        "server": server,
        "caldav": caldav,
        "auth": auth,
        "protocols": {"ews": ews, "eas": eas} if eas else {"ews": ews},
        "features": features,
        "users": users,
    }
    return result


def validate_document(document: Mapping[str, Any], env: Mapping[str, str] | None = None) -> None:
    """Runs the production validator. The privacy acknowledgement is assumed present for this check only (the converter never sets it)."""
    probe = copy.deepcopy(dict(document))
    eas = probe.get("protocols", {}).get("eas")
    if eas and eas.get("enabled") is True:
        eas["acknowledge_mobile_cloud_processing"] = True
    from_dict(probe, env or {})


def _read_source(path: Path) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise MigrationError(f"source file not found: {path}") from None
    except OSError as e:
        raise MigrationError(f"cannot open source file {path} (symbolic links are not followed): {e.strerror}") from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
        os.close(fd)
        raise MigrationError(f"source is not a regular file: {path}" if not stat.S_ISREG(info.st_mode) else "source file is larger than 1 MiB")
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_SOURCE_BYTES + 1)
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise MigrationError(f"source is not valid TOML: {e}") from None


def write_new_file(path: Path, text: str) -> None:
    """Atomic, mode 0600, never overwrites, never follows a symbolic link at the target."""
    if os.path.lexists(path):
        raise MigrationError(f"refusing to overwrite existing target: {path}")
    parent = path.parent if str(path.parent) else Path(".")
    if not parent.is_dir():
        raise MigrationError(f"target directory does not exist: {parent}")
    temp = parent / f".{path.name}.tmp-{os.getpid()}"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            raise MigrationError(f"refusing to overwrite existing target: {path}") from None
    except OSError as e:
        raise MigrationError(f"cannot write {path}: {e.strerror}") from None
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass


def migrate_files(source: Path, target: Path, *, feature_profile: str | None = None, env: Mapping[str, str] | None = None) -> MigrationResult:
    result = migrate_legacy(_read_source(Path(source)), feature_profile=feature_profile)
    try:
        validate_document(result.document, env)
    except ConfigError as e:
        raise MigrationError(f"the converted configuration is not valid: {e}") from None
    header = "DAVbridge configuration (config_version = 1), converted from a legacy configuration file.\nReview it, then validate with: davbridge config validate"
    write_new_file(Path(target), toml_writer.dumps(result.document, header))
    return result


def format_report(result: MigrationResult, source: Path, target: Path) -> str:
    users = result.document["users"]
    lines = [f"Converted {source} -> {target} (mode 0600)", f"  users: {len(users)}, calendars: {sum(len(u['calendars']) for u in users)}"]
    lines += [f"  note: {n}" for n in result.notes]
    if result.obsolete:
        lines.append("Not carried over (obsolete, unsafe or fixed):")
        lines += [f"  - {o}" for o in result.obsolete]
    if result.actions:
        lines.append("Action required:")
        lines += [f"  - {a}" for a in result.actions]
    lines.append("The old SQLite state and HMAC key are not converted; run `davbridge init` for a fresh key. Clients rebuild their view once.")
    return "\n".join(lines)
