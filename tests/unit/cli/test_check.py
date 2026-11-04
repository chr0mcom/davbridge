"""`davbridge check`: configuration, state directory, CalDAV server, users and public endpoints against a fake CalDAV transport."""

import base64
import copy
import io
import json
import os
import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest
from tests.conftest import TEST_CONFIG, mix

from davbridge import check, cli
from davbridge.store import SCHEMA, SCHEMA_EAS
from davbridge.toml_writer import dumps

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "caldav"
SECRET = "pw-test-1234"
OTHER_SECRET = "pw-bob-5678"
READ_ONLY = "read-only-calendar_shared_by_read-only-owner"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeServer:
    """A CalDAV server as an httpx transport: OPTIONS, principal, calendar home and single calendars. Records every request."""

    def __init__(self) -> None:
        self.passwords = {"test-user": SECRET, "bob": OTHER_SECRET}
        self.calendars: dict[tuple[str, str], dict] = {
            ("test-user", "test-calendar"): {"writable": True, "components": ("VEVENT",)},
            ("test-user", READ_ONLY): {"writable": False, "components": ("VEVENT",)},
        }
        self.options_status = 401
        self.options_headers = {"DAV": "1, 3, extended-mkcol, calendar-access"}
        self.home = fixture("home.xml")
        self.failure: Exception | None = None
        self.requests: list[httpx.Request] = []

    def authenticated(self, request: httpx.Request) -> str | None:
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return None
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        return user if self.passwords.get(user) == password else None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        path = request.url.path
        if request.method == "OPTIONS":
            return httpx.Response(self.options_status, headers=self.options_headers)
        user = self.authenticated(request)
        if user is None:
            return httpx.Response(401)
        assert request.method == "PROPFIND"
        if path == f"/remote.php/dav/principals/users/{user}/":
            return httpx.Response(207, content=fixture("principal.xml").replace("test-user", user).encode())
        if path == f"/remote.php/dav/calendars/{user}/":
            return httpx.Response(207, content=self.home.replace("test-user", user).encode())
        parts = path.strip("/").split("/")
        entry = self.calendars.get((user, parts[-1])) if len(parts) == 5 and parts[3] == user else None
        if entry is None:
            return httpx.Response(404)
        privileges = "<d:privilege><d:read/></d:privilege>" + ("<d:privilege><d:write/></d:privilege>" if entry["writable"] else "")
        components = "".join(f'<c:comp name="{c}"/>' for c in entry["components"])
        body = (
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>' + path + "</d:href><d:propstat><d:prop>"
            f"<d:displayname>Name</d:displayname><d:sync-token>http://sabre.io/ns/sync/3</d:sync-token><d:current-user-privilege-set>{privileges}</d:current-user-privilege-set>"
            f"<c:supported-calendar-component-set>{components}</c:supported-calendar-component-set></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"
        )
        return httpx.Response(207, content=body.encode())


class PublicServer:
    def __init__(self, **answers) -> None:
        self.answers = answers
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        known = {
            "/EWS/Exchange.asmx": httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="dav.example.com"'}),
            "/Microsoft-Server-ActiveSync": httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="dav.example.com"'}),
        }
        known.update(self.answers)
        return known.get(request.url.path, httpx.Response(404))


@pytest.fixture
def environment(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    config = tmp_path / "davbridge.toml"
    config.write_text(dumps(TEST_CONFIG))
    for name in [n for n in os.environ if n.startswith("DAVBRIDGE")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("DAVBRIDGE_CONFIG", str(config))
    monkeypatch.setenv("DAVBRIDGE_STATE_DIR", str(state))
    assert cli.main(["init"]) == 0
    return tmp_path


@pytest.fixture
def server(monkeypatch) -> FakeServer:
    s = FakeServer()
    install(monkeypatch, s)
    return s


def install(monkeypatch, handler) -> None:
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs.pop("transport", None)
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(check.httpx, "AsyncClient", factory)


def stdin(monkeypatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def run(argv, capsys, expect_secrets=False):
    code = cli.main(["check", *argv])
    out = capsys.readouterr()
    if not expect_secrets:
        assert SECRET not in out.out + out.err and OTHER_SECRET not in out.out + out.err
    return code, out.out, out.err


def run_json(argv, capsys):
    code, out, _ = run([*argv, "--json"], capsys)
    document = json.loads(out)
    assert document["exit_code"] == code
    return code, document


def levels(document) -> dict[str, str]:
    return {f["id"]: f["level"] for f in document["findings"]}


def write_config(environment, **sections):
    path = environment / "alt.toml"
    path.write_text(dumps(mix(copy.deepcopy(TEST_CONFIG), sections)))
    return path


# ------------------------------------------------------------------ a healthy installation


def test_healthy_installation_exits_with_0_and_reports_each_area(environment, server, capsys):
    code, out, _ = run([], capsys)
    assert code == 0
    for text in (
        "Configuration is valid",
        "EWS (Outlook for Mac) is enabled",
        "EAS (Outlook for iOS/Android) is disabled",
        "State directory",
        "is writable",
        "hmac.key",
        "CalDAV server cloud.example.com is reachable",
        "Users were not tested",
    ):
        assert text in out, text
    assert out.rstrip().endswith("Result: ok (0 error(s), 0 warning(s))")
    assert [r.method for r in server.requests] == ["OPTIONS"]


def test_json_output_has_a_versioned_schema(environment, server, capsys):
    code, document = run_json([], capsys)
    assert code == 0 and document["schema_version"] == 1 and document["result"] == "ok"
    assert set(document) == {"schema_version", "version", "result", "exit_code", "findings"}
    assert all(set(f) == {"id", "level", "message"} and f["level"] in ("ok", "info", "warning", "error") for f in document["findings"])
    assert {"config.valid", "state.writable", "state.hmac-key", "caldav.reachable", "runtime.version"} <= set(levels(document))


def test_check_is_read_only(environment, server, capsys, monkeypatch):
    state = environment / "state"
    before = sorted(p.name for p in state.iterdir())
    stdin(monkeypatch, SECRET + "\n")
    run(["--user", "test-user", "--password-stdin"], capsys)
    assert sorted(p.name for p in state.iterdir()) == before and "davbridge.sqlite" not in before
    assert {r.method for r in server.requests} <= {"OPTIONS", "PROPFIND"}


def test_show_effective_config(environment, server, capsys):
    code, out, _ = run(["--show-effective-config"], capsys)
    assert code == 0 and "[server]" in out and 'public_url = "https://dav.example.com"' in out
    code, document = run_json(["--show-effective-config"], capsys)
    assert document["effective_config"]["server"]["public_url"] == "https://dav.example.com"


# ------------------------------------------------------------------ configuration


def test_invalid_configuration_exits_with_2_without_network(environment, server, capsys):
    (environment / "davbridge.toml").write_text("config_version = 1\n")
    code, out, _ = run([], capsys)
    assert code == 2 and "Configuration" in out and "FAIL" in out
    assert server.requests == []


def test_missing_configuration_file_is_an_error(environment, server, capsys):
    code, out, _ = run(["--config", str(environment / "missing.toml")], capsys)
    assert code == 2 and "not found" in out


def test_environment_overrides_are_validated_too(environment, server, capsys, monkeypatch):
    monkeypatch.setenv("DAVBRIDGE_CALDAV_URL", "http://insecure.example.com/remote.php/dav")
    code, out, _ = run([], capsys)
    assert code == 2 and "https" in out


def test_warnings_exit_with_1(environment, server, capsys, monkeypatch):
    path = write_config(environment, caldav={"verify_tls": False})
    code, document = run_json(["--config", str(path)], capsys)
    assert code == 1 and levels(document)["config.tls-verification-disabled"] == "warning"
    monkeypatch.setenv("DAVBRIDGE_ALLOW_INSECURE_HTTP", "true")
    code, document = run_json([], capsys)
    assert code == 1 and levels(document)["config.insecure-http"] == "warning"


def test_pattern_mode_gets_a_clear_warning(environment, server, capsys):
    path = write_config(environment, auth={"mode": "pattern", "username_pattern": "^[a-z]+$"}, dynamic_users={"calendar_mode": "auto"})
    code, out, _ = run(["--config", str(path)], capsys)
    assert code == 1 and "ANY CalDAV user" in out and "WARN" in out


def test_eas_state_and_acknowledgement_are_reported(environment, server, capsys):
    raw = copy.deepcopy(TEST_CONFIG)
    raw["users"][0]["eas_enabled"] = True
    raw["protocols"] = {"eas": {"enabled": True, "acknowledge_mobile_cloud_processing": True}}
    (environment / "eas.toml").write_text(dumps(raw))
    code, out, _ = run(["--config", str(environment / "eas.toml")], capsys)
    assert code == 0 and "EAS 14.1 (Outlook for iOS/Android) is enabled" in out and "acknowledged" in out
    raw["protocols"]["eas"]["acknowledge_mobile_cloud_processing"] = False
    (environment / "eas.toml").write_text(dumps(raw))
    code, out, _ = run(["--config", str(environment / "eas.toml")], capsys)
    assert code == 2 and "acknowledge_mobile_cloud_processing" in out


# ------------------------------------------------------------------ state directory


def test_missing_state_directory_and_key_are_errors(environment, server, capsys):
    (environment / "state" / "hmac.key").unlink()
    code, document = run_json([], capsys)
    assert code == 2 and levels(document)["state.hmac-key"] == "error"
    (environment / "state" / "hmac.key").write_text("short")
    code, document = run_json([], capsys)
    assert code == 2 and levels(document)["state.hmac-key"] == "error"
    shutil.rmtree(environment / "state")
    code, document = run_json([], capsys)
    assert code == 2 and levels(document)["state.missing"] == "error"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_read_only_state_directory_is_an_error(environment, server, capsys):
    os.chmod(environment / "state", 0o500)
    try:
        code, document = run_json([], capsys)
    finally:
        os.chmod(environment / "state", 0o700)
    assert code == 2 and levels(document)["state.not-writable"] == "error"


def test_loose_state_directory_permissions_are_a_warning(environment, server, capsys):
    os.chmod(environment / "state", 0o755)
    code, document = run_json([], capsys)
    found = levels(document)
    assert code == 1 and found["state.permissions"] == "warning" and found["state.hmac-key"] == "ok"


def test_a_group_or_world_accessible_key_is_an_error(environment, server, capsys):
    os.chmod(environment / "state" / "hmac.key", 0o640)
    code, document = run_json([], capsys)
    assert code == 2 and levels(document)["state.hmac-key"] == "error"
    assert "accessible to group or others" in json.dumps(document)


def test_a_symbolic_link_as_key_is_an_error(environment, server, capsys):
    key = environment / "state" / "hmac.key"
    real = environment / "elsewhere.key"
    key.rename(real)
    key.symlink_to(real)
    code, document = run_json([], capsys)
    assert code == 2 and levels(document)["state.hmac-key"] == "error" and "symbolic link" in json.dumps(document)


def test_a_key_that_is_not_a_regular_file_is_an_error(environment, server, capsys):
    key = environment / "state" / "hmac.key"
    key.unlink()
    key.mkdir()
    code, document = run_json([], capsys)
    assert code == 2 and levels(document)["state.hmac-key"] == "error" and "not a regular file" in json.dumps(document)


def make_database(environment, version):
    path = environment / "state" / "davbridge.sqlite"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA + SCHEMA_EAS)
    db.execute(f"PRAGMA user_version={version}")
    db.commit()
    db.close()
    os.chmod(path, 0o600)
    return path


def test_database_schema_is_checked(environment, server, capsys):
    from davbridge.store import SCHEMA_VERSION

    make_database(environment, SCHEMA_VERSION)
    code, document = run_json([], capsys)
    assert code == 0 and levels(document)["state.database"] == "ok"
    db = sqlite3.connect(environment / "state" / "davbridge.sqlite")
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
    db.commit()
    db.close()
    code, document = run_json([], capsys)
    assert code == 1 and levels(document)["state.schema"] == "warning"
    db = sqlite3.connect(environment / "state" / "davbridge.sqlite")
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 4}")
    db.commit()
    db.close()
    code, document = run_json([], capsys)
    assert code == 2 and levels(document)["state.schema"] == "error" and "newer DAVbridge" in json.dumps(document)


def test_corrupt_database_is_an_error(environment, server, capsys):
    (environment / "state" / "davbridge.sqlite").write_bytes(b"this is not a database" * 100)
    code, document = run_json([], capsys)
    assert code == 2 and levels(document)["state.database"] == "error"


# ------------------------------------------------------------------ CalDAV server


def test_unreachable_server_is_an_error_and_stops_the_user_tests(environment, server, capsys, monkeypatch):
    server.failure = httpx.ConnectError("no route")
    stdin(monkeypatch, SECRET + "\n")
    code, out, _ = run(["--user", "test-user", "--password-stdin"], capsys)
    assert code == 2 and "connection failed" in out and "signed in" not in out


def test_tls_problems_are_named(environment, server, capsys):
    server.failure = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    code, out, _ = run([], capsys)
    assert code == 2 and "TLS verification failed" in out


def test_timeout_is_named(environment, server, capsys):
    server.failure = httpx.ConnectTimeout("slow")
    code, out, _ = run([], capsys)
    assert code == 2 and "timed out" in out


@pytest.mark.parametrize(("status", "word"), [(302, "redirect"), (301, "redirect"), (503, "HTTP 503")])
def test_redirects_and_server_errors_are_errors(environment, server, capsys, status, word):
    server.options_status = status
    code, out, _ = run([], capsys)
    assert code == 2 and word in out


def test_missing_calendar_capability_is_a_warning(environment, server, capsys):
    server.options_headers = {"DAV": "1, 3"}
    code, document = run_json([], capsys)
    assert code == 1 and levels(document)["caldav.capabilities"] == "warning"


# ------------------------------------------------------------------ users and calendars


def test_user_sign_in_and_calendars_with_effective_rights(environment, server, capsys, monkeypatch):
    stdin(monkeypatch, SECRET + "\n")
    code, out, _ = run(["--user", "test-user", "--password-stdin"], capsys)
    assert code == 0
    assert "user test-user: signed in at the CalDAV server" in out
    assert "calendar test-calendar (test-calendar): found, supports events, effective rights: writable" in out
    assert 'effective rights: read-only (write = "never")' in out


def test_server_side_read_only_and_owner_rules_are_reported(environment, server, capsys, monkeypatch):
    server.calendars[("test-user", "test-calendar")]["writable"] = False
    stdin(monkeypatch, SECRET + "\n")
    code, out, _ = run(["--user", "test-user", "--password-stdin"], capsys)
    assert code == 0 and "read-only (the server grants no write privilege)" in out


def test_wrong_password_names_the_user_but_never_the_secret(environment, server, capsys, monkeypatch):
    stdin(monkeypatch, "not-the-password-0000\n")
    code, out, err = run(["--user", "test-user", "--password-stdin"], capsys)
    assert code == 2 and "user test-user: the CalDAV server rejected the credentials" in out
    assert "not-the-password-0000" not in out + err


def test_unknown_user_is_refused_without_a_caldav_request(environment, server, capsys, monkeypatch):
    stdin(monkeypatch, "bob:" + OTHER_SECRET + "\n")
    code, out, _ = run(["--password-stdin"], capsys)
    assert code == 2 and "user bob: not allowed by the configuration" in out
    assert [r.method for r in server.requests] == ["OPTIONS"]


def test_several_users_report_which_ones_fail(environment, server, capsys, monkeypatch):
    raw = copy.deepcopy(TEST_CONFIG)
    raw["auth"]["allowed_users"] = ["test-user", "bob"]
    raw["users"].append({"username": "bob", "calendars": [{"path": "bob-cal", "id": "bob-cal", "default": True}]})
    (environment / "two.toml").write_text(dumps(raw))
    server.calendars[("bob", "bob-cal")] = {"writable": True, "components": ("VEVENT",)}
    stdin(monkeypatch, f"test-user:{SECRET}\nbob:wrong-password-9999\n")
    code, document = run_json(["--config", str(environment / "two.toml"), "--password-stdin"], capsys)
    found = levels(document)
    assert code == 2 and found["user.test-user.auth"] == "ok" and found["user.bob.auth"] == "error"
    assert "wrong-password-9999" not in json.dumps(document)


def test_missing_calendar_and_calendar_without_events_are_errors(environment, server, capsys, monkeypatch):
    del server.calendars[("test-user", "test-calendar")]
    server.calendars[("test-user", READ_ONLY)]["components"] = ("VTODO",)
    stdin(monkeypatch, SECRET + "\n")
    code, out, _ = run(["--user", "test-user", "--password-stdin"], capsys)
    assert code == 2 and "the calendar does not exist for this user" in out and "does not support events" in out


def test_password_prompt_rules(environment, server, capsys, monkeypatch):
    with pytest.raises(SystemExit):
        cli.main(["check", "--user", "!!bad"])
    assert cli.main(["check", "--user", "test-user"]) == 2
    assert "--password-stdin" in capsys.readouterr().err
    for text in ("", "\n\n", "a\nb\n"):
        stdin(monkeypatch, text)
        assert cli.main(["check", "--user", "test-user", "--password-stdin"]) == 2
    capsys.readouterr()
    for text in ("", "no-colon\n", ":pw\n", "bad name:pw\n"):
        stdin(monkeypatch, text)
        assert cli.main(["check", "--password-stdin"]) == 2
    err = capsys.readouterr().err
    assert SECRET not in err and "error:" in err


# ------------------------------------------------------------------ autodiscovery


def auto_config(environment, default="personal", **extra):
    raw = copy.deepcopy(TEST_CONFIG)
    raw["users"][0].pop("calendars")
    raw["users"][0]["calendar_mode"] = "auto"
    raw["dynamic_users"] = {"calendar_mode": "auto", "default_calendar": default, **extra}
    path = environment / "auto.toml"
    path.write_text(dumps(raw))
    return str(path)


def test_autodiscovery_lists_candidates_and_the_default(environment, server, capsys, monkeypatch):
    stdin(monkeypatch, SECRET + "\n")
    code, out, _ = run(["--config", auto_config(environment), "--user", "test-user", "--password-stdin"], capsys)
    assert code == 0
    assert "4 calendar candidate(s)" in out and "personal (writable)" in out and "contact_birthdays (read-only)" in out and "team_shared_by_bob" not in out
    assert "autodiscovery works; default calendar is personal" in out


def test_autodiscovery_with_shared_calendars_opt_in(environment, server, capsys, monkeypatch):
    stdin(monkeypatch, SECRET + "\n")
    code, out, _ = run(["--config", auto_config(environment, include_shared=True), "--user", "test-user", "--password-stdin"], capsys)
    assert code == 0 and "team_shared_by_bob (read-only, shared)" in out and "shared calendars included" in out


@pytest.mark.parametrize("rule", ["only", "missing-calendar"])
def test_ambiguous_or_unknown_default_calendar_is_an_error(environment, server, capsys, monkeypatch, rule):
    stdin(monkeypatch, SECRET + "\n")
    code, out, _ = run(["--config", auto_config(environment, default=rule), "--user", "test-user", "--password-stdin"], capsys)
    assert code == 2 and "no default calendar can be chosen" in out


def test_user_without_calendars_cannot_log_in(environment, server, capsys, monkeypatch):
    server.home = fixture("home.xml").split("<d:response>")[0] + "</d:multistatus>"
    stdin(monkeypatch, SECRET + "\n")
    code, out, _ = run(["--config", auto_config(environment), "--user", "test-user", "--password-stdin"], capsys)
    assert code == 2 and "no readable calendar with event support" in out


def test_pattern_user_is_tested_by_name(environment, server, capsys, monkeypatch):
    path = write_config(environment, auth={"mode": "pattern", "username_pattern": "^[a-z-]+$"}, dynamic_users={"calendar_mode": "auto", "default_calendar": "personal"})
    stdin(monkeypatch, OTHER_SECRET + "\n")
    code, out, _ = run(["--config", str(path), "--user", "bob", "--password-stdin"], capsys)
    assert code == 1 and "user bob: autodiscovery works; default calendar is personal" in out


def test_pattern_user_is_tested_under_the_servers_spelling(environment, server, capsys, monkeypatch):
    path = write_config(environment, auth={"mode": "pattern", "username_pattern": "^[A-Za-z-]+$"}, dynamic_users={"calendar_mode": "auto", "default_calendar": "personal"})
    server.passwords["Bob"] = OTHER_SECRET

    async def canonical(self, z):
        return "bob" if z.user == "Bob" else None

    monkeypatch.setattr(check.HttpCaldav, "canonical_user", canonical)
    stdin(monkeypatch, OTHER_SECRET + "\n")
    code, out, _ = run(["--config", str(path), "--user", "Bob", "--password-stdin"], capsys)
    assert "the server knows this user as bob" in out and "user bob: autodiscovery works; default calendar is personal" in out


# ------------------------------------------------------------------ public URL


def test_public_probe_expects_basic_challenges(environment, capsys, monkeypatch):
    install(monkeypatch, PublicServer())
    code, out, _ = run(["--public"], capsys)
    assert "answers 401 with a Basic challenge" in out and "/healthz is not publicly routable" in out and "does not prove" in out
    assert code in (0, 2)


def test_public_probe_flags_a_bearer_challenge_and_open_health_endpoint(environment, capsys, monkeypatch):
    server = FakeServer()
    public = PublicServer(
        **{"/EWS/Exchange.asmx": httpx.Response(401, headers={"WWW-Authenticate": "Bearer realm=x"}), "/healthz": httpx.Response(200, text="ok"), "/davbridge-check-unknown-path": httpx.Response(200)}
    )

    def both(request):
        return public(request) if request.url.host == "dav.example.com" else server(request)

    install(monkeypatch, both)
    code, document = run_json(["--public"], capsys)
    found = levels(document)
    assert code == 2 and found["public.ews"] == "error" and found["public.healthz"] == "warning" and found["public.unknown-path"] == "warning"


def test_public_probe_reports_unreachable_host(environment, capsys, monkeypatch):
    server = FakeServer()

    def both(request):
        if request.url.host == "dav.example.com":
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]")
        return server(request)

    install(monkeypatch, both)
    code, out, _ = run(["--public"], capsys)
    assert code == 2 and "dav.example.com/EWS/Exchange.asmx: TLS verification failed" in out
