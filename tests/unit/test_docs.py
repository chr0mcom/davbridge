"""Documentation consistency: what the documents promise must match the code (CLI, configuration schema, environment variables,
log events, capture format, database schema)."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from davbridge import cli, config, protocol_capture, store

ROOT = Path(__file__).resolve().parents[2]
DOC_FILES = ["README.md", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md", *sorted(str(p.relative_to(ROOT)) for p in (ROOT / "docs").rglob("*.md"))]
DOCS = {name: (ROOT / name).read_text(encoding="utf-8") for name in DOC_FILES}
ALL_DOCS = "\n".join(DOCS.values())
CONFIGURATION = DOCS["docs/configuration.md"]
SRC = {p: p.read_text(encoding="utf-8") for p in sorted((ROOT / "src" / "davbridge").rglob("*.py"))}


def subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def options(parser: argparse.ArgumentParser) -> list[str]:
    return [o for a in parser._actions for o in a.option_strings if o.startswith("--") and o != "--help"]


def commands() -> list[tuple[str, argparse.ArgumentParser]]:
    found: list[tuple[str, argparse.ArgumentParser]] = []
    for name, sub in subparsers(cli.build_parser()).items():
        nested = subparsers(sub)
        if nested:
            found.extend((f"{name} {inner}", p) for inner, p in nested.items())
        else:
            found.append((name, sub))
    return found


# ------------------------------------------------------------------ CLI


@pytest.mark.parametrize(("name", "parser"), commands(), ids=[c[0] for c in commands()])
def test_every_cli_command_and_option_is_documented(name, parser):
    assert f"davbridge {name}" in ALL_DOCS, f"`davbridge {name}` is not mentioned in the documentation"
    missing = [o for o in options(parser) if o not in ALL_DOCS]
    assert not missing, f"`davbridge {name}`: undocumented options {missing}"


def code_spans(text: str) -> list[str]:
    spans = re.findall(r"`([^`\n]+)`", text)
    fenced = re.findall(r"```[a-z]*\n(.*?)```", text, flags=re.S)
    return spans + [line for block in fenced for line in block.splitlines()]


def test_documented_davbridge_commands_exist():
    top = set(subparsers(cli.build_parser()))
    nested = {name for name, _ in commands() if " " in name}
    seen = set()
    for name, text in DOCS.items():
        for span in code_spans(text):
            for match in re.finditer(r"(?<![\w./:-])davbridge (?!davbridge\b)([a-z][a-z-]*)(?: ([a-z]+))?", span):
                first, second = match.group(1), match.group(2)
                assert first in top, f"{name}: unknown command in `{span.strip()}`"
                if first in {n.split()[0] for n in nested}:
                    assert f"{first} {second}" in nested, f"{name}: unknown sub-command in `{span.strip()}`"
                seen.add(first)
    assert {"init", "check", "backup", "migrate", "rotate-key", "reset", "unblock", "config"} <= seen


def test_eas_test_client_subcommands_and_options_are_documented():
    spec = importlib.util.spec_from_file_location("eas_test_client_for_docs", ROOT / "tools" / "eas_test_client.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parser = module.parser()
    contributing = DOCS["CONTRIBUTING.md"]
    names = set(subparsers(parser))
    assert {"sync-full", "all", "clear-state", "readonly", "series"} <= names
    for name in names:
        assert re.search(rf"`{re.escape(name)}\b", contributing), f"eas_test_client subcommand {name} is not documented"
    every_option = {o for p in [parser, *subparsers(parser).values()] for o in options(p)}
    assert {"--password-stdin", "--window", "--folder", "--state"} <= every_option
    for option in sorted(every_option):
        assert option in contributing, f"eas_test_client option {option} is not documented"
    assert all(re.fullmatch(r"[a-z][a-z-]*", n) for n in names), "subcommand names must be English words"


# ------------------------------------------------------------------ configuration


def flatten(document: dict, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    keys: list[tuple[str, ...]] = []
    for key, value in document.items():
        if isinstance(value, dict):
            keys.extend(flatten(value, prefix + (key,)))
        elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            for item in value:
                keys.extend(flatten(item, prefix + (key,)))
        else:
            keys.append(prefix + (key,))
    return keys


def schema_keys() -> set[tuple[str, ...]]:
    keys = {section + (key,) for section, table in config._SECTIONS.items() for key in table}
    keys |= {("users", key) for key in config._USER_KEYS} | {("users", "calendars", key) for key in config._CALENDAR_KEYS}
    keys |= {(key,) for key in config._TOP_KEYS}
    return keys


EXAMPLE = ROOT / "config" / "davbridge.example.toml"


def test_every_key_of_the_example_configuration_is_known_to_the_schema_and_documented():
    document = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    known = schema_keys()
    for key in flatten(document):
        assert key in known, f"example key {'.'.join(key)} is not part of the schema"
        assert key[-1] in CONFIGURATION, f"example key {'.'.join(key)} is not documented in docs/configuration.md"
    config.load(EXAMPLE, {})


def test_every_schema_key_is_documented_with_its_section():
    for key in sorted(schema_keys()):
        if len(key) == 1 or key == ("users", "calendars"):
            continue
        assert f"`{key[-1]}`" in CONFIGURATION, f"schema key {'.'.join(key)} is not documented in docs/configuration.md"
    for section in config._SECTIONS:
        assert f"[{'.'.join(section)}]" in CONFIGURATION, f"section [{'.'.join(section)}] is not documented"
    assert "[[users]]" in CONFIGURATION and "[[users.calendars]]" in CONFIGURATION


def test_example_covers_every_active_eas_key():
    document = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert set(document["protocols"]["eas"]) == set(config._SECTIONS[("protocols", "eas")])


def test_every_environment_variable_of_the_code_is_documented():
    names = set()
    for path, text in SRC.items():
        names |= set(re.findall(r"\b(DAVBRIDGE_[A-Z][A-Z_]*[A-Z])\b", text))
    names.discard("DAVBRIDGE_")
    assert {"DAVBRIDGE_LOGS", "DAVBRIDGE_ALLOW_INSECURE_HTTP", "DAVBRIDGE_STATE_DIR", "DAVBRIDGE_CONFIG", "DAVBRIDGE_LOG_LEVEL"} <= names
    for name in sorted(names):
        assert name in CONFIGURATION, f"{name} is not documented in docs/configuration.md"


def test_documented_defaults_match_the_code():
    eas = config.EasConfig()
    for key, value in {
        "wait_max_minutes": eas.wait_max_minutes,
        "ping_limit_per_user": eas.ping_limit_per_user,
        "device_expiry_days": eas.device_expiry_days,
        "request_max_seconds": eas.request_max_seconds,
        "request_body_max_bytes": eas.request_body_max_bytes,
        "response_max_bytes": eas.response_max_bytes,
        "body_max_chars": eas.body_max_chars,
    }.items():
        row = next(line for line in CONFIGURATION.splitlines() if line.startswith(f"| `{key}`"))
        assert f"`{value}`" in row, f"default of {key} in the documentation differs from the code ({value})"
    example = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["protocols"]["eas"]
    assert all(
        example[k] == getattr(eas, k)
        for k in ("wait_max_minutes", "ping_limit_per_user", "device_expiry_days", "request_max_seconds", "request_body_max_bytes", "response_max_bytes", "body_max_chars")
    )
    assert config.DynamicConfig().calendar_mode == "explicit"
    assert any(line.startswith("| `calendar_mode`") and '`"explicit"` | `"auto"` is the opt-in' in line.replace("\\|", "|") for line in CONFIGURATION.splitlines())


# ------------------------------------------------------------------ vocabulary


def log_event_kinds() -> set[str]:
    kinds = set()
    for text in SRC.values():
        kinds |= set(re.findall(r"\.(?:event|error)\(\s*\"([a-z_]+)\"", text))
    kinds.add("request")
    return kinds


def test_event_names_use_underscores_and_exist():
    kinds = log_event_kinds()
    row = next(line for line in DOCS["docs/troubleshooting.md"].splitlines() if line.startswith("| `event`"))
    documented = set(re.findall(r"`([a-z_]+)`", row.split("|")[2]))
    assert documented and documented <= kinds, f"unknown event names in docs/troubleshooting.md: {documented - kinds}"
    for kind in kinds:
        assert "-" not in kind
    for doc in DOCS.values():
        assert not re.findall(r"`(?:folder-created|change-rejected|version-rejected|login-rejected|raw-request|stream-end)`", doc)


def test_no_rollout_stage_vocabulary_outside_the_legacy_section():
    section = CONFIGURATION.index("## Converting a legacy configuration")
    for name, text in DOCS.items():
        scanned = text[:section] if name == "docs/configuration.md" else text
        found = re.findall(r"\b(?:Stufe|stufe|M[0-3])\b", scanned)
        assert not found, f"{name} mentions rollout stages: {found}"
    for name, text in DOCS.items():
        assert "/bereit" not in text, name


def test_removed_or_internal_names_do_not_appear_in_the_documents():
    section = CONFIGURATION.index("## Converting a legacy configuration")
    for name, text in DOCS.items():
        scanned = text[:section] if name == "docs/configuration.md" else text
        for forbidden in ("bereit", "messmodus", "Messmodus", "delete_ohne_changekey"):
            assert forbidden not in scanned, f"{name}: {forbidden}"


def test_schema_version_and_tables_are_documented():
    architecture = DOCS["docs/architecture.md"]
    assert re.search(rf"currently \*\*{store.SCHEMA_VERSION}\*\*", architecture)
    assert re.search(rf"schema {store.SCHEMA_VERSION}\b", DOCS["docs/operations.md"])
    assert re.search(rf"schema version {store.SCHEMA_VERSION}\b", DOCS["CHANGELOG.md"])
    for table in re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", store.SCHEMA + store.SCHEMA_EAS):
        assert f"`{table}`" in architecture, f"table {table} is not documented in docs/architecture.md"


class Collect:
    def __init__(self) -> None:
        self.entries: list[tuple[Path, str, bool]] = []

    def write(self, path, text, *, one_time=False):
        self.entries.append((Path(path), text, one_time))


def test_documented_capture_layout_and_keys_match_the_code():
    from tests.unit.ews.test_protocol_capture import FakeClock, create_item

    architecture = DOCS["docs/architecture.md"]
    storage = Collect()
    capture = protocol_capture.ProtocolCapture(Path("/state/capture"), FakeClock(), storage)
    skeleton = protocol_capture.skeleton(create_item(), {}, datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc))
    asyncio.run(capture.capture(SimpleNamespace(name="test-user", test=True, capture=True), skeleton, ["NoError"], 5))
    line = next(json.loads(t) for _, t, once in storage.entries if not once)
    skeleton_file = next(p for p, _, once in storage.entries if once)
    document = next(json.loads(t) for _, t, once in storage.entries if once)
    assert skeleton_file.parent.name == "skeletons" and "capture/skeletons/<fingerprint>.json" in architecture
    for key in [*line, *document, "name", "attribute", "children"]:
        assert f"`{key}`" in architecture, f"capture key {key} is not documented in docs/architecture.md"
    eas_source = (ROOT / "src" / "davbridge" / "eas" / "http.py").read_text(encoding="utf-8")
    for key in ("time", "command", "status", "codes", "header", "request", "response"):
        assert f'"{key}":' in eas_source and f"`{key}`" in architecture
    assert "capture/eas-YYYY-MM-DD.jsonl" in architecture


def test_privacy_document_mentions_the_raw_ip_addresses_and_how_to_clear_them():
    privacy = DOCS["docs/privacy.md"]
    assert re.search(r"raw\s+(?:\*\*)?(?:client\s+)?IP", privacy, re.I) or "raw IP" in privacy.replace("\n", " ")
    assert "davbridge unblock" in privacy and "lockout" in privacy and "24 hours" in privacy


def test_docs_are_ascii_english_without_german_wording():
    german = re.compile(r"\b(?:nicht|und|oder|keine|wird|werden|Br\u00fccke|Kalender|Termin|Datenschutz|Betreiber|Konto)\b")
    for name, text in DOCS.items():
        for number, line in enumerate(text.splitlines(), 1):
            if name == "docs/configuration.md" and ("konto" in line or "kalender" in line):
                continue
            assert not german.search(line), f"{name}:{number} contains German wording"


def test_every_specification_revision_is_recorded_with_a_date():
    import re

    text = (ROOT / "docs" / "protocol-sources.md").read_text(encoding="utf-8")
    assert "not recorded" not in text.lower()
    rows = [line for line in text.splitlines() if re.match(r"\| MS-[A-Z]+", line)]
    assert len(rows) >= 20
    for row in rows:
        assert re.search(r"\d+\.\d+(?:\.\d+)? \(\d{4}-\d{2}-\d{2}\)|v\d{8}", row), row
