"""`server.listen` takes effect: the `serve` command binds uvicorn to it and `healthcheck` probes it."""

import copy

import httpx
import pytest
from tests.conftest import TEST_CONFIG, mix

from davbridge import cli
from davbridge.config import ConfigError, from_dict, listen_address
from davbridge.store import SCHEMA, SCHEMA_EAS
from davbridge.toml_writer import dumps


@pytest.fixture
def environment(tmp_path, monkeypatch):
    import os

    for name in [n for n in os.environ if n.startswith("DAVBRIDGE")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("DAVBRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DAVBRIDGE_CONFIG", "placeholder")
    monkeypatch.delenv("DAVBRIDGE_CONFIG")
    assert cli.main(["init"]) == 0
    return tmp_path


def write_config(path, listen):
    path.write_text(dumps(mix(copy.deepcopy(TEST_CONFIG), {"server": {"listen": listen}} if listen else {})))
    return path


@pytest.mark.parametrize(
    ("listen", "expected"),
    [("0.0.0.0:8000", ("0.0.0.0", 8000)), ("127.0.0.1:9123", ("127.0.0.1", 9123)), ("[::1]:8080", ("::1", 8080)), ("[::]:80", ("::", 80)), ("proxy-net.local:65535", ("proxy-net.local", 65535))],
)
def test_listen_address_parses_host_and_port(listen, expected):
    assert listen_address(listen) == expected


@pytest.mark.parametrize("listen", ["", "8000", ":8000", "host:", "host:0", "host:65536", "host:port", "::1:8000", "[::1:8000", "[host]:80", "a b:80", "host/x:80"])
def test_invalid_listen_values_are_rejected_by_the_validator(listen):
    with pytest.raises(ValueError):
        listen_address(listen)
    with pytest.raises(ConfigError, match="server.listen"):
        from_dict(mix(copy.deepcopy(TEST_CONFIG), {"server": {"listen": listen}}))


def test_serve_binds_to_the_configured_address(environment, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_run_server", lambda host, port: calls.append((host, port)))
    path = write_config(environment / "c.toml", "127.0.0.1:9123")
    assert cli.main(["serve", "--config", str(path)]) == 0
    assert calls == [("127.0.0.1", 9123)]
    monkeypatch.setenv("DAVBRIDGE_CONFIG", str(write_config(environment / "d.toml", None)))
    assert cli.main(["serve"]) == 0
    assert calls[-1] == ("0.0.0.0", 8000)
    monkeypatch.setenv("DAVBRIDGE__SERVER__LISTEN", "[::]:8443")
    assert cli.main(["serve"]) == 0
    assert calls[-1] == ("::", 8443)


@pytest.mark.parametrize("damage", ["missing", "mode", "symlink", "directory"])
def test_serve_refuses_to_start_with_an_unsafe_key_file(environment, monkeypatch, capsys, damage):
    started = []
    monkeypatch.setattr(cli, "_run_server", lambda host, port: started.append(1))
    path = write_config(environment / "c.toml", None)
    key = environment / "hmac.key"
    if damage == "missing":
        key.unlink()
    elif damage == "mode":
        key.chmod(0o644)
    elif damage == "symlink":
        key.rename(environment / "real.key")
        key.symlink_to(environment / "real.key")
    else:
        key.unlink()
        key.mkdir()
    assert cli.main(["serve", "--config", str(path)]) == 2
    assert "hmac.key" in capsys.readouterr().err and not started


def test_serve_refuses_a_newer_or_foreign_state_database_with_exit_2(environment, monkeypatch, capsys):
    import sqlite3

    started = []
    monkeypatch.setattr(cli, "_run_server", lambda host, port: started.append(1))
    path = write_config(environment / "c.toml", None)
    database = environment / "davbridge.sqlite"
    db = sqlite3.connect(database)
    db.executescript(SCHEMA + SCHEMA_EAS)
    db.execute("PRAGMA user_version=99")
    db.commit()
    db.close()
    assert cli.main(["serve", "--config", str(path)]) == 2
    assert "newer DAVbridge" in capsys.readouterr().err
    database.unlink()
    db = sqlite3.connect(database)
    db.execute("CREATE TABLE notes(a)")
    db.commit()
    db.close()
    assert cli.main(["serve", "--config", str(path)]) == 2
    assert "not a DAVbridge state database" in capsys.readouterr().err and not started


def test_serve_starts_with_a_fresh_or_current_database(environment, monkeypatch):
    started = []
    monkeypatch.setattr(cli, "_run_server", lambda host, port: started.append(1))
    path = write_config(environment / "c.toml", None)
    assert cli.main(["serve", "--config", str(path)]) == 0 and started == [1]


def test_serve_hands_the_config_option_to_the_application(environment, monkeypatch):
    import os

    from davbridge.config import config_path

    seen = []
    monkeypatch.setattr(cli, "_run_server", lambda host, port: seen.append(config_path(os.environ)))
    other = write_config(environment / "other.toml", "127.0.0.1:9123")
    monkeypatch.chdir(environment)
    assert cli.main(["serve", "--config", "other.toml"]) == 0
    assert seen == [other.resolve()]
    monkeypatch.setenv("DAVBRIDGE_CONFIG", str(write_config(environment / "env.toml", None)))
    assert cli.main(["serve"]) == 0
    assert seen[-1] == environment / "env.toml"


def test_serve_starts_exactly_one_uvicorn_worker_with_the_listen_values(monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(app=app, **kwargs))
    cli._run_server("127.0.0.1", 9123)
    assert seen["app"] == "davbridge.main:app" and seen["host"] == "127.0.0.1" and seen["port"] == 9123
    assert seen["workers"] == 1 and seen["proxy_headers"] is False and seen["access_log"] is False and seen["server_header"] is False


def test_serve_refuses_an_invalid_configuration_with_exit_2(environment, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(cli, "_run_server", lambda host, port: started.append(1))
    bad = environment / "bad.toml"
    bad.write_text("config_version = 1\n")
    assert cli.main(["serve", "--config", str(bad)]) == 2
    assert "error:" in capsys.readouterr().err and not started


def test_healthcheck_probes_the_configured_port(environment, monkeypatch):
    urls = []

    def get(url, **kwargs):
        urls.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(cli.httpx, "get", get)
    assert cli.main(["healthcheck", "--config", str(write_config(environment / "a.toml", "0.0.0.0:9123"))]) == 0
    assert cli.main(["healthcheck", "--config", str(write_config(environment / "b.toml", "[::]:9124"))]) == 0
    assert cli.main(["healthcheck", "--config", str(write_config(environment / "c.toml", "10.1.2.3:9125"))]) == 0
    assert urls == ["http://127.0.0.1:9123/healthz", "http://[::1]:9124/healthz", "http://10.1.2.3:9125/healthz"]


def test_healthcheck_ignores_proxy_environment_variables(environment, monkeypatch):
    seen = {}

    def get(url, **kwargs):
        seen.update(kwargs)
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(cli.httpx, "get", get)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    assert cli.main(["healthcheck", "--config", str(write_config(environment / "a.toml", None))]) == 0
    assert seen["trust_env"] is False


def test_healthcheck_fails_when_unhealthy_or_unreachable(environment, monkeypatch, capsys):
    path = str(write_config(environment / "a.toml", None))
    monkeypatch.setattr(cli.httpx, "get", lambda url, **kw: httpx.Response(503, request=httpx.Request("GET", url)))
    assert cli.main(["healthcheck", "--config", path]) == 1
    assert "503" in capsys.readouterr().err

    def refuse(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(cli.httpx, "get", refuse)
    assert cli.main(["healthcheck", "--config", path]) == 1
