"""Public `davbridge` command line: commands, exit codes, English help, no secrets in the output."""

import asyncio
import base64
import json
import os
import stat
import time
from pathlib import Path

import httpx
import pytest
from tests.conftest import TEST_CONFIG

from davbridge import __version__, cli
from davbridge.clock import SystemClock
from davbridge.config import read_key
from davbridge.store import EasSync, Store
from davbridge.toml_writer import dumps

EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "davbridge.example.toml"
LEGACY = Path(__file__).resolve().parents[2] / "fixtures" / "config" / "legacy-neutral.toml"
SECRET = "app-password-must-never-appear"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "davbridge.toml"
    config.write_text(dumps(TEST_CONFIG))
    for name in [n for n in os.environ if n.startswith("DAVBRIDGE")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("DAVBRIDGE_CONFIG", str(config))
    monkeypatch.setenv("DAVBRIDGE_STATE_DIR", str(state))
    monkeypatch.setenv("DAVBRIDGE_CALDAV_PASSWORD", SECRET)
    return tmp_path


def run(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    return code, out.out, out.err


def prepare(environment):
    async def loose():
        s = await Store.open(environment / "state" / "davbridge.sqlite", SystemClock())
        assert await s.eas_device_see("t", "aaaaaaaaaaaa", "iPhone", "Outlook-iOS/2.0", 6) == "new"
        assert await s.eas_device_see("u", "bbbbbbbbbbbb", "Android", "Outlook-Android/2.0", 6) == "new"
        await s.eas_sync_write(EasSync("t", "aaaaaaaaaaaa", "k", 3, None, {}, {}, None, None, {}, 0), expected=None)
        for _ in range(10):
            await s.lock_failed_attempt("eas:1.2.3.4")
        for _ in range(10):
            await s.lock_failed_attempt("1.2.3.4")
        await s.lock_failed_attempt("5.6.7.8")
        await s.new_state("t", "k", "tok", {}, {})
        await s.new_state("t", "other", "tok", {}, {})
        await s.new_state("u", "k", "tok", {}, {})
        await s.close()

    asyncio.run(loose())


async def count(environment):
    s = await Store.open(environment / "state" / "davbridge.sqlite", SystemClock())
    try:
        return await s.count_states(), await s.locks_active(), len(await s.eas_devices())
    finally:
        await s.close()


# ------------------------------------------------------------------ help, version, usage


def test_version(environment, capsys, monkeypatch):
    code, out, _ = run(["version"], capsys)
    assert code == 0 and out.strip() == f"davbridge {__version__}"
    monkeypatch.setenv("GIT_COMMIT", "0123456789abcdef0123")
    assert run(["version"], capsys)[1].strip() == f"davbridge {__version__} (commit 0123456789ab)"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["reset"],
        ["config"],
        ["reset", "sync"],
        ["bogus"],
        ["unblock"],
        ["unblock", "--ip", "1.2.3.4", "--all"],
        ["devices", "--user", "a/b"],
        ["reset", "devices", "--user", "t", "--device", "NOT-A-HASH"],
        ["reset", "sync", "--user", "t", "--calendar", "Bad Id"],
        ["eas-geraete"],
        ["nc"],
        ["pruefe"],
        ["latenz"],
        ["messung-bericht"],
        ["sperre-aufheben"],
        ["zustand-verwerfen"],
    ],
)
def test_calls_without_or_with_wrong_arguments_exit_with_2(argv, environment, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2
    assert SECRET not in capsys.readouterr().err


def all_helpers():
    parser = cli.build_parser()
    yield ["--help"]
    for command in ("version", "init", "serve", "healthcheck", "check", "backup", "migrate", "rotate-key", "devices", "unblock", "diagnose"):
        yield [command, "--help"]
    for target in ("sync", "devices", "all"):
        yield ["reset", target, "--help"]
    yield ["reset", "--help"]
    for under in ("validate", "migrate"):
        yield ["config", under, "--help"]
    assert parser.prog == "davbridge"


def test_help_is_english_and_shows_only_the_public_commands(capsys):
    german = ("konto", "ordner", "geraet", "sperre", "zustand", "passwort", "aufheben", "verwerfen", "pruefe", "latenz", "messung", "kalender ")
    for argv in all_helpers():
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 0
        text = capsys.readouterr().out
        assert text.isascii() and "usage: davbridge" in text
        assert not [w for w in german if w in text.lower()], (argv, text)
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    top = capsys.readouterr().out
    for command in ("version", "init", "serve", "healthcheck", "check", "backup", "migrate", "rotate-key", "devices", "reset", "unblock", "diagnose", "config"):
        assert command in top
    for removed in ("nc", "pruefe", "latenz", "messung-bericht", "eas-geraete", "sperre-aufheben", "hash"):
        assert f" {removed} " not in top.replace("\n", " ") + " ", removed


# ------------------------------------------------------------------ init


def test_init_creates_key_atomically_with_0600_and_does_not_print_it(environment, capsys):
    code, out, err = run(["init"], capsys)
    target = environment / "state" / "hmac.key"
    assert code == 0 and target.is_file() and stat.S_IMODE(target.stat().st_mode) == 0o600
    key = target.read_text().strip()
    assert len(base64.b64decode(key, validate=True)) == 32 and len(read_key(target)) == 32
    assert key not in out + err and str(target) in out and "secret" in out
    assert sorted(p.name for p in (environment / "state").iterdir()) == ["hmac.key"]


def test_init_never_overwrites(environment, capsys):
    run(["init"], capsys)
    target = environment / "state" / "hmac.key"
    before = target.read_bytes()
    code, out, err = run(["init"], capsys)
    assert code == 1 and "refusing to overwrite" in err and out == "" and target.read_bytes() == before
    target.unlink()
    target.symlink_to(environment / "elsewhere")
    code, _, err = run(["init"], capsys)
    assert code == 1 and "refusing to overwrite" in err and not (environment / "elsewhere").exists()


def test_init_creates_the_state_directory_and_nothing_else(environment, capsys, monkeypatch):
    new = environment / "fresh" / "state"
    monkeypatch.setenv("DAVBRIDGE_STATE_DIR", str(new))
    assert run(["init"], capsys)[0] == 0
    assert [p.name for p in new.iterdir()] == ["hmac.key"]


def test_init_reports_non_writable_directory(environment, capsys, monkeypatch):
    file = environment / "afile"
    file.write_text("x")
    monkeypatch.setenv("DAVBRIDGE_STATE_DIR", str(file / "sub"))
    code, _, err = run(["init"], capsys)
    assert code == 1 and err.startswith("error: ") and err.isascii()


# ------------------------------------------------------------------ devices, reset, unblock


def test_devices(environment, capsys):
    prepare(environment)
    code, out, _ = run(["devices"], capsys)
    lines = out.splitlines()
    assert code == 0 and lines[0].split()[:3] == ["USER", "DEVICE", "TYPE"] and len(lines) == 3
    assert "aaaaaaaaaaaa" in out and "bbbbbbbbbbbb" in out and "k:3" in out
    code, out, _ = run(["devices", "--user", "u"], capsys)
    assert "bbbbbbbbbbbb" in out and "aaaaaaaaaaaa" not in out
    assert run(["devices", "--user", "nobody"], capsys)[1].strip() == "No devices."


def test_commands_without_state_database_fail_with_1(environment, capsys):
    for argv in (["devices"], ["reset", "sync", "--user", "t"], ["reset", "devices", "--user", "t"], ["reset", "all", "--yes-i-understand"], ["unblock", "--all"], ["unblock", "--ip", "1.2.3.4"]):
        code, out, err = run(argv, capsys)
        assert code == 1 and "no state database" in err and out == ""
    assert not (environment / "state" / "davbridge.sqlite").exists()


def test_reset_sync(environment, capsys):
    prepare(environment)
    code, out, _ = run(["reset", "sync", "--user", "t", "--calendar", "other"], capsys)
    assert code == 0 and "Discarded 1 EWS sync state" in out and asyncio.run(count(environment))[0] == 2
    code, out, _ = run(["reset", "sync", "--user", "t"], capsys)
    assert "Discarded 1 EWS sync state" in out and asyncio.run(count(environment))[0] == 1


def test_reset_devices(environment, capsys):
    prepare(environment)
    code, out, _ = run(["reset", "devices", "--user", "t", "--device", "cccccccccccc"], capsys)
    assert code == 0 and "Discarded 0" in out and asyncio.run(count(environment))[2] == 2
    code, out, _ = run(["reset", "devices", "--user", "t", "--device", "aaaaaaaaaaaa"], capsys)
    assert "Discarded 2" in out and asyncio.run(count(environment))[2] == 1
    assert run(["reset", "devices", "--user", "u"], capsys)[0] == 0 and asyncio.run(count(environment))[2] == 0


def test_reset_all_requires_confirmation_and_never_deletes_key_or_configuration(environment, capsys):
    prepare(environment)
    run(["init"], capsys)
    key = (environment / "state" / "hmac.key").read_bytes()
    config = (environment / "davbridge.toml").read_bytes()
    code, _, err = run(["reset", "all"], capsys)
    assert code == 2 and "--yes-i-understand" in err and asyncio.run(count(environment)) == (3, 2, 2)
    code, out, _ = run(["reset", "all", "--yes-i-understand"], capsys)
    assert code == 0 and "hmac.key and the configuration were not touched" in out
    assert asyncio.run(count(environment)) == (0, 0, 0)
    assert (environment / "state" / "hmac.key").read_bytes() == key and (environment / "davbridge.toml").read_bytes() == config
    assert (environment / "state" / "davbridge.sqlite").is_file()


def test_unblock(environment, capsys):
    prepare(environment)
    code, out, _ = run(["unblock", "--ip", "1.2.3.4"], capsys)
    assert code == 0 and "Removed 2 lock" in out and asyncio.run(count(environment))[1] == 0
    assert asyncio.run(_locks(environment)) == 1
    code, out, _ = run(["unblock", "--all"], capsys)
    assert code == 0 and "Removed 1 lock" in out and asyncio.run(_locks(environment)) == 0


async def _locks(environment):
    s = await Store.open(environment / "state" / "davbridge.sqlite", SystemClock())
    try:
        return len(await s.locks_list())
    finally:
        await s.close()


# ------------------------------------------------------------------ diagnose

REAL_CLIENT = httpx.AsyncClient


def mock_http(monkeypatch, status=401):
    real = REAL_CLIENT

    def factory(*a, **k):
        k.pop("verify", None)
        return real(*a, transport=httpx.MockTransport(lambda r: httpx.Response(status, request=r)), **k)

    monkeypatch.setattr(cli.httpx, "AsyncClient", factory)


def test_diagnosis_ok_and_problem(environment, capsys, monkeypatch):
    prepare(environment)
    (environment / "state" / "status.json").write_text(json.dumps({"time": int(time.time()), "subscriptions": 2, "streams": 1}))
    mock_http(monkeypatch, 401)
    code, out, _ = run(["diagnose"], capsys)
    line = out.splitlines()[0]
    assert code == 0 and line.startswith("status=ok ") and "caldav=reachable" in line and "subscriptions=2" in line and "sync_states=3" in line
    assert json.loads(out.split("\n", 1)[1])["streams"] == 1
    code, out, _ = run(["diagnose", "--short"], capsys)
    assert len(out.splitlines()) == 1
    mock_http(monkeypatch, 503)
    code, out, _ = run(["diagnose", "--short"], capsys)
    assert code == 1 and "status=problem" in out and "caldav=degraded" in out
    (environment / "state" / "status.json").unlink()
    mock_http(monkeypatch, 200)
    code, out, _ = run(["diagnose", "--short"], capsys)
    assert code == 1 and "status_age_seconds=None" in out
    code, out, _ = run(["diagnose"], capsys)
    assert code == 1 and len(out.splitlines()) == 1 and "{" not in out


def test_diagnosis_without_valid_configuration_exits_with_2(environment, capsys):
    prepare(environment)
    (environment / "davbridge.toml").write_text("config_version = 1\n")
    code, _, err = run(["diagnose"], capsys)
    assert code == 2 and err.startswith("error: ")


# ------------------------------------------------------------------ config validate / migrate


def test_config_validate_example_file(environment, capsys):
    code, out, err = run(["config", "validate", "--config", str(EXAMPLE)], capsys)
    assert code == 0 and out.startswith("Configuration OK") and "1 user(s)" in out and err == ""


def test_config_validate_uses_davbridge_config_and_environment(environment, capsys, monkeypatch):
    assert run(["config", "validate"], capsys)[0] == 0
    monkeypatch.setenv("DAVBRIDGE_EAS_ENABLED", "maybe")
    code, _, err = run(["config", "validate"], capsys)
    assert code == 2 and "DAVBRIDGE_EAS_ENABLED" in err
    monkeypatch.delenv("DAVBRIDGE_EAS_ENABLED")
    monkeypatch.setenv("DAVBRIDGE_PUBLIC_URL", "http://dav.example.com")
    code, _, err = run(["config", "validate"], capsys)
    assert code == 2 and "https" in err


@pytest.mark.parametrize(
    "text,keyword",
    [("config_version = 1\n[server]\npublic_url = 'https://d.example.com'\nbogus = 1\n", "unknown key server.bogus"), ("[server]\n", "config_version"), ("config_version = [", "invalid TOML")],
)
def test_config_validate_errors_are_english_with_exit_2(environment, capsys, text, keyword):
    (environment / "bad.toml").write_text(text)
    code, out, err = run(["config", "validate", "--config", str(environment / "bad.toml")], capsys)
    assert code == 2 and out == "" and keyword in err and err.isascii()
    code, _, err = run(["config", "validate", "--config", str(environment / "missing.toml")], capsys)
    assert code == 2 and "not found" in err


def test_config_validate_effective_config_is_loadable_again(environment, capsys, monkeypatch):
    import tomllib

    monkeypatch.setenv("DAVBRIDGE__CALDAV__READ_TIMEOUT_SECONDS", "77")
    code, out, _ = run(["config", "validate", "--show-effective-config"], capsys)
    assert code == 0
    doc = tomllib.loads(out.split("\n\n", 1)[1])
    assert doc["caldav"]["read_timeout_seconds"] == 77 and doc["config_version"] == 1 and doc["users"][0]["username"] == "test-user"
    from davbridge.config import from_dict

    assert from_dict(doc).read_timeout_seconds == 77


def test_config_validate_warns_in_pattern_mode(environment, capsys):
    r = json.loads(json.dumps(TEST_CONFIG))
    r["auth"].update({"mode": "pattern", "username_pattern": "^[a-z]+$"})
    r["dynamic_users"] = {"calendar_mode": "auto"}
    (environment / "p.toml").write_text(dumps(r))
    code, out, err = run(["config", "validate", "--config", str(environment / "p.toml")], capsys)
    assert code == 0 and "Configuration OK" in out and "pattern" in err and "any" in err.lower()


def test_config_migrate(environment, capsys):
    target = environment / "new.toml"
    code, out, err = run(["config", "migrate", "--from", str(LEGACY), "--to", str(target)], capsys)
    assert code == 0 and "Converted" in out and "Action required" in out and stat.S_IMODE(target.stat().st_mode) == 0o600
    code, out, err = run(["config", "migrate", "--from", str(LEGACY), "--to", str(target)], capsys)
    assert code == 2 and "refusing to overwrite" in err and out == ""


def test_config_migrate_stage_and_feature_profile(environment, capsys):
    old = environment / "old.toml"
    old.write_text(LEGACY.read_text().replace('stufe = "M2"', 'stufe = "M0"'))
    code, _, err = run(["config", "migrate", "--from", str(old), "--to", str(environment / "n.toml")], capsys)
    assert code == 2 and "--feature-profile" in err and not (environment / "n.toml").exists()
    code, out, _ = run(["config", "migrate", "--from", str(old), "--to", str(environment / "n.toml"), "--feature-profile", "basic"], capsys)
    assert code == 0 and "feature profile: basic" in out
    with pytest.raises(SystemExit) as e:
        cli.main(["config", "migrate", "--from", str(old), "--to", str(environment / "m.toml"), "--feature-profile", "m2"])
    assert e.value.code == 2


def test_config_migrate_loads_the_result_with_the_validator(environment, capsys):
    target = environment / "new.toml"
    run(["config", "migrate", "--from", str(LEGACY), "--to", str(target)], capsys)
    code, _, err = run(["config", "validate", "--config", str(target)], capsys)
    assert code == 2 and "acknowledge_mobile_cloud_processing" in err
