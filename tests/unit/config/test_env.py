"""Environment overrides: named variables, DAVBRIDGE__SECTION__KEY, JSON lists, strict booleans, precedence."""

import copy

import pytest
from tests.conftest import TEST_CONFIG

from davbridge.config import ConfigError, apply_environment, load
from davbridge.toml_writer import dumps


@pytest.fixture
def file(tmp_path):
    path = tmp_path / "davbridge.toml"
    path.write_text(dumps(TEST_CONFIG))
    return path


def k(file, **env):
    return load(file, env)


def test_without_environment_the_file_applies(file):
    c = k(file)
    assert c.public_url == "https://dav.example.com" and c.state_dir.as_posix() == "/var/lib/davbridge" and c.logs_dir is None
    assert c.log_level == "info"


def test_named_variables_override_the_file(file):
    c = k(
        file,
        DAVBRIDGE_PUBLIC_URL="https://other.example.org",
        DAVBRIDGE_CALDAV_URL="https://nc.example.org/remote.php/dav",
        DAVBRIDGE_TRUSTED_PROXIES='["10.1.0.0/16", "10.2.0.5/32"]',
        DAVBRIDGE_ADDRESS_TEMPLATE="{user}@other.example.org",
    )
    assert c.public_url == "https://other.example.org" and c.caldav_base_url == "https://nc.example.org/remote.php/dav"
    assert c.trusted_proxies == ("10.1.0.0/16", "10.2.0.5/32") and c.address_template == "{user}@other.example.org"


def test_allowed_users_and_user_pattern(file):
    c = k(file, DAVBRIDGE_ALLOWED_USERS='["test-user"]')
    assert [x.name for x in c.accounts] == ["test-user"]
    with pytest.raises(ConfigError, match="no .*users.* block"):
        k(file, DAVBRIDGE_ALLOWED_USERS='["test-user", "bob"]')
    c = k(file, DAVBRIDGE_ALLOWED_USERS='["test-user", "bob"]', DAVBRIDGE_CALENDAR_MODE="auto")
    assert [x.name for x in c.accounts] == ["test-user", "bob"] and c.accounts[1].auto
    with pytest.raises(ConfigError, match="calendar_mode"):
        k(file, DAVBRIDGE_USER_PATTERN="^[a-z]+$", DAVBRIDGE__AUTH__MODE="pattern")
    c = k(file, DAVBRIDGE_USER_PATTERN="^[a-z]+$", DAVBRIDGE__AUTH__MODE="pattern", DAVBRIDGE_CALENDAR_MODE="auto")
    assert c.auth_mode == "pattern" and c.username_pattern == "^[a-z]+$"


def test_calendar_mode_for_dynamic_users(file):
    root = {}
    apply_environment(root, {"DAVBRIDGE_CALENDAR_MODE": "explicit"})
    assert root == {"dynamic_users": {"calendar_mode": "explicit"}}


def test_state_log_and_loglevel(file, tmp_path):
    c = k(file, DAVBRIDGE_STATE_DIR=str(tmp_path / "s"), DAVBRIDGE_LOGS=str(tmp_path / "l"), DAVBRIDGE_LOG_LEVEL="WARNING")
    assert c.state_dir == tmp_path / "s" and c.logs_dir == tmp_path / "l" and c.log_level == "warning"
    with pytest.raises(ConfigError, match="DAVBRIDGE_LOG_LEVEL"):
        k(file, DAVBRIDGE_LOG_LEVEL="verbose")


def test_double_underscore_keys_and_json_lists(file):
    c = k(
        file,
        DAVBRIDGE__CALDAV__READ_TIMEOUT_SECONDS="60",
        DAVBRIDGE__PROTOCOLS__EWS__STREAM_MAX_SECONDS="900",
        DAVBRIDGE__CALDAV__VERIFY_TLS="false",
        DAVBRIDGE__FEATURES__READ_ONLY_OWNERS='["a", "b"]',
        DAVBRIDGE__SERVER__DEFAULT_TIMEZONE="Asia/Tokyo",
        DAVBRIDGE__PROTOCOLS__EWS__EVENT_DETAIL="folder",
    )
    assert c.read_timeout_seconds == 60 and c.stream_max_seconds == 900 and c.verify_tls is False
    assert c.read_only_owners == frozenset({"a", "b"}) and c.default_timezone == "Asia/Tokyo" and c.event_detail == "folder"


@pytest.mark.parametrize(
    "name", ["DAVBRIDGE__UNKNOWN", "DAVBRIDGE__CALDAV__NOPE", "DAVBRIDGE__SERVER", "DAVBRIDGE__PROTOCOLS__TCP__X", "DAVBRIDGE____X", "DAVBRIDGE__USERS__0__USERNAME", "DAVBRIDGE__CONFIG_VERSION"]
)
def test_unknown_double_underscore_variables_are_errors(file, name):
    with pytest.raises(ConfigError, match="unknown environment variable"):
        k(file, **{name: "x"})


@pytest.mark.parametrize("value", ["True", "TRUE", "1", "0", "yes", "", " true", "true "])
def test_boolean_variables_are_strict(file, value):
    for name in ("DAVBRIDGE_EAS_ENABLED", "DAVBRIDGE_ACKNOWLEDGE_MOBILE_CLOUD_PROCESSING", "DAVBRIDGE__CALDAV__VERIFY_TLS"):
        with pytest.raises(ConfigError, match="'true' or 'false'"):
            k(file, **{name: value})


@pytest.mark.parametrize("value", ["a,b", "[", "{}", '"x"', "5", "null"])
def test_lists_are_json_not_comma_separated(file, value):
    with pytest.raises(ConfigError, match="JSON list"):
        k(file, DAVBRIDGE_TRUSTED_PROXIES=value)


@pytest.mark.parametrize("value", ["abc", "1.5", "", "0x10", "1e3"])
def test_integer_variables_are_strict(file, value):
    with pytest.raises(ConfigError, match="integer"):
        k(file, DAVBRIDGE__CALDAV__READ_TIMEOUT_SECONDS=value)


def test_precedence_short_variable_over_double_underscore_over_file(file):
    assert k(file).public_url == "https://dav.example.com"
    c = k(file, DAVBRIDGE__SERVER__PUBLIC_URL="https://general.example.org")
    assert c.public_url == "https://general.example.org"
    c = k(file, DAVBRIDGE__SERVER__PUBLIC_URL="https://general.example.org", DAVBRIDGE_PUBLIC_URL="https://named.example.org")
    assert c.public_url == "https://named.example.org"


def test_eas_via_environment_still_needs_all_switches(file):
    r = copy.deepcopy(TEST_CONFIG)
    r["users"][0]["eas_enabled"] = True
    file.write_text(dumps(r))
    with pytest.raises(ConfigError, match="eas_enabled"):
        k(file)
    with pytest.raises(ConfigError, match="acknowledge_mobile_cloud_processing"):
        k(file, DAVBRIDGE_EAS_ENABLED="true")
    c = k(file, DAVBRIDGE_EAS_ENABLED="true", DAVBRIDGE_ACKNOWLEDGE_MOBILE_CLOUD_PROCESSING="true")
    assert c.eas.enabled and c.eas.acknowledged


def test_environment_does_not_change_the_file_and_contains_no_secrets(file):
    before = file.read_text()
    c = k(file, DAVBRIDGE_CALDAV_PASSWORD="should-never-be-read", DAVBRIDGE_PASSWORD="nope")
    assert file.read_text() == before
    assert "should-never-be-read" not in repr(c) and "nope" not in repr(c)
