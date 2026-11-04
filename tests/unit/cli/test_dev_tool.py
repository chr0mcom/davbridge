"""tools/dev_calendar.py is a developer tool and requires explicit test accounts, a title prefix and a password on stdin."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOOL = ROOT / "tools" / "dev_calendar.py"


def run(*args, stdin=""):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run([sys.executable, str(TOOL), *args], input=stdin, capture_output=True, text=True, env=env, timeout=60)


def test_help_is_english_and_names_the_protection_rules():
    r = run("--help")
    assert r.returncode == 0 and "DAVBRIDGE-TEST" in r.stdout and "--password-stdin" in r.stdout and r.stdout.isascii()


def test_user_must_be_named_explicitly(tmp_path):
    r = run("--config", str(tmp_path / "c.toml"), "event", "create", "--test", "DAVBRIDGE-TEST-1")
    assert r.returncode != 0 and "--user is required" in r.stderr


def test_without_password_on_stdin_nothing_is_attempted(tmp_path):
    from tests.conftest import TEST_CONFIG

    from davbridge.toml_writer import dumps

    (tmp_path / "c.toml").write_text(dumps(TEST_CONFIG))
    r = run("--config", str(tmp_path / "c.toml"), "--user", "test-user", "event", "create", "--test", "DAVBRIDGE-TEST-1")
    assert r.returncode != 0 and "--password-stdin is required" in r.stderr
    r = run("--config", str(tmp_path / "c.toml"), "--user", "unknown", "--password-stdin", "event", "create", "--test", "DAVBRIDGE-TEST-1", stdin="x\n")
    assert r.returncode != 0 and "not configured" in r.stderr
    r = run("--config", str(tmp_path / "c.toml"), "--user", "test-user", "--password-stdin", "event", "create", "--test", "ordinary title", stdin="x\n")
    assert r.returncode != 0 and "DAVBRIDGE-TEST" in r.stderr


def test_test_commands_do_not_exist_in_the_public_cli():
    from davbridge import cli

    text = cli.build_parser().format_help()
    for name in ("pruefe", "latenz", "messung-bericht", " nc "):
        assert name not in text
