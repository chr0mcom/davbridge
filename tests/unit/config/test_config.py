"""Configuration schema (config_version = 1): defaults, strict validation, user and calendar rules, EAS switches."""

import copy

import pytest
from tests.conftest import TEST_CONFIG

from davbridge.config import ConfigError, FeatureSet, account_for, check_start, dynamic_account, from_dict, load


def raw(**paths):
    """TESTKONFIG with changes: keyword `a__b__c=value` sets ["a"]["b"]["c"]; `users__0__x` indexes lists."""
    r = copy.deepcopy(TEST_CONFIG)
    for path, value in paths.items():
        node = r
        parts = path.split("__")
        for part in parts[:-1]:
            node = node[int(part)] if part.isdigit() else node.setdefault(part, {})
        node[int(parts[-1]) if parts[-1].isdigit() else parts[-1]] = value
    return r


def error(r, keyword):
    with pytest.raises(ConfigError) as e:
        from_dict(r)
    assert keyword in str(e.value), str(e.value)
    assert str(e.value).isascii()


def test_valid_configuration_and_default_values():
    k = from_dict(raw())
    assert k.features is FeatureSet.DEFAULT and len(k.accounts) == 1
    assert k.accounts[0].default_calendar().key == "test-calendar"
    assert k.accounts[0].calendar[1].write == "never" and k.accounts[0].calendar[0].write == "caldav"
    assert "read-only-owner" in k.read_only_owners
    assert k.trusted_proxies == () and k.auth_mode == "allowlist" and k.ews_enabled is True
    assert k.eas.enabled is False and k.eas.acknowledged is False and k.listen == "0.0.0.0:8000"
    assert k.verify_tls is True and k.follow_redirects is False and (k.connect_timeout_seconds, k.read_timeout_seconds) == (5, 30)
    assert (k.positive_cache_seconds, k.failure_limit, k.initial_lock_seconds) == (300, 10, 900)


def test_minimal_config_uses_safe_defaults():
    k = from_dict(
        {
            "config_version": 1,
            "server": {"public_url": "https://dav.example.com"},
            "caldav": {"base_url": "https://cloud.example.com/remote.php/dav"},
            "users": [{"username": "alice", "calendars": [{"path": "personal", "id": "primary", "default": True}]}],
        }
    )
    assert k.stream_max_seconds == 1800 and k.event_detail == "both" and k.default_timezone == "UTC" and k.autodiscover_culture == "en:us"
    assert k.read_only_owners == frozenset() and k.protected_property_prefixes == () and k.trusted_proxies == ()
    assert k.accounts[0].address == "alice@dav.example.com" and k.accounts[0].display_name == "alice" and k.accounts[0].eas is False
    assert k.accounts[0].calendar[0].owner == "alice" and k.accounts[0].calendar[0].write == "caldav"
    assert k.event_fields == ("subject", "location", "time", "all_day", "body", "busy_status", "reminder", "categories", "sensitivity")
    assert k.protected_writable_uris == frozenset({"item:Subject", "calendar:Location", "calendar:Start", "calendar:End", "calendar:IsAllDayEvent", "calendar:StartTimeZone", "calendar:EndTimeZone"})


def test_config_version_is_mandatory_and_strict():
    r = raw()
    del r["config_version"]
    error(r, "config_version")
    error(raw(config_version=2), "config_version")
    error(raw(config_version=True), "config_version")
    error(raw(config_version="1"), "config_version")


@pytest.mark.parametrize("path", ["extra", "server__extra", "caldav__timeout", "auth__extra", "protocols__ews__extra", "protocols__eas__extra", "protocols__tcp", "features__extra", "users__0__extra"])
def test_unknown_keys_are_fatal(path):
    error(raw(**{path: 1}), "unknown key")


def test_unknown_calendar_key_is_fatal():
    r = raw()
    r["users"][0]["calendars"][0]["colour"] = "red"
    error(r, "unknown key")


@pytest.mark.parametrize(
    "path,value",
    [
        ("caldav__verify_tls", "true"),
        ("caldav__verify_tls", 1),
        ("caldav__follow_redirects", "no"),
        ("caldav__connect_timeout_seconds", True),
        ("caldav__read_timeout_seconds", "30"),
        ("caldav__read_timeout_seconds", 3.5),
        ("auth__failure_limit", "10"),
        ("auth__failure_limit", True),
        ("protocols__ews__enabled", "yes"),
        ("protocols__ews__stream_max_seconds", "540"),
        ("protocols__ews__stream_max_seconds", True),
        ("server__public_url", 5),
        ("server__trusted_proxies", "10.0.0.1/32"),
        ("server__trusted_proxies", [1]),
        ("auth__allowed_users", "test-user"),
        ("features__event_fields", "subject"),
        ("features__read_only_owners", [1]),
        ("users__0__eas_enabled", "false"),
        ("users__0__display_name", 5),
    ],
)
def test_wrong_types_are_fatal(path, value):
    with pytest.raises(ConfigError):
        from_dict(raw(**{path: value}))


@pytest.mark.parametrize(
    "path,value",
    [
        ("caldav__connect_timeout_seconds", 0),
        ("caldav__read_timeout_seconds", 601),
        ("auth__failure_limit", 0),
        ("auth__initial_lock_seconds", 0),
        ("auth__positive_cache_seconds", -1),
        ("protocols__ews__stream_max_seconds", 59),
        ("protocols__ews__stream_max_seconds", 1801),
        ("protocols__ews__event_detail", "all"),
        ("auth__mode", "open"),
    ],
)
def test_ranges_and_enumerations(path, value):
    with pytest.raises(ConfigError):
        from_dict(raw(**{path: value}))


@pytest.mark.parametrize(
    "path,url",
    [
        ("server__public_url", "http://dav.example.com"),
        ("caldav__base_url", "http://cloud.example.com/remote.php/dav"),
        ("caldav__base_url", "ftp://cloud.example.com"),
        ("caldav__base_url", "cloud.example.com"),
        ("server__public_url", "https://dav.example.com/path"),
        ("server__public_url", "https://user:pw@dav.example.com"),
        ("caldav__base_url", "https://cloud.example.com/dav?x=1"),
        ("server__public_url", "https://"),
    ],
)
def test_only_https_urls(path, url):
    with pytest.raises(ConfigError):
        from_dict(raw(**{path: url}))


def test_http_only_in_explicit_development_mode():
    r = raw(caldav__base_url="http://127.0.0.1:8080/remote.php/dav")
    assert from_dict(r, {"DAVBRIDGE_ALLOW_INSECURE_HTTP": "true"}).caldav_base_url == "http://127.0.0.1:8080/remote.php/dav"
    with pytest.raises(ConfigError):
        from_dict(r, {"DAVBRIDGE_ALLOW_INSECURE_HTTP": "false"})
    with pytest.raises(ConfigError):
        from_dict(r, {"DAVBRIDGE_ALLOW_INSECURE_HTTP": "1"})


def test_url_without_trailing_slash():
    assert from_dict(raw(caldav__base_url="https://cloud.example.com/remote.php/dav/")).caldav_base_url == "https://cloud.example.com/remote.php/dav"
    assert from_dict(raw(server__public_url="https://dav.example.com/")).public_url == "https://dav.example.com"


@pytest.mark.parametrize("zone", ["Europe/Berlin", "America/New_York", "UTC", "Asia/Tokyo"])
def test_default_timezone_is_adopted(zone):
    assert from_dict(raw(server__default_timezone=zone)).default_timezone == zone


@pytest.mark.parametrize("zone", ["Mars/Olympus", "", 5])
def test_invalid_default_timezone_is_fatal(zone):
    with pytest.raises(ConfigError):
        from_dict(raw(server__default_timezone=zone))


def test_autodiscover_culture_configurable():
    assert from_dict(raw(server__autodiscover_culture="de:de")).autodiscover_culture == "de:de"
    for bad in ("de", "DE:DE", "de-de", "", 5):
        with pytest.raises(ConfigError):
            from_dict(raw(server__autodiscover_culture=bad))


@pytest.mark.parametrize(
    "listen,valid", [("0.0.0.0:8000", True), ("127.0.0.1:9000", True), ("[::]:8000", True), (":8000", False), ("0.0.0.0", False), ("0.0.0.0:0", False), ("0.0.0.0:70000", False), ("host:abc", False)]
)
def test_listen(listen, valid):
    if valid:
        assert from_dict(raw(server__listen=listen)).listen == listen
    else:
        with pytest.raises(ConfigError):
            from_dict(raw(server__listen=listen))


def test_trusted_proxies():
    k = from_dict(raw(server__trusted_proxies=["172.18.0.2/32", "10.0.0.0/8", "fd00::/8", "192.168.1.5", "172.18.0.2/32"]))
    assert k.trusted_proxies == ("172.18.0.2/32", "10.0.0.0/8", "fd00::/8", "192.168.1.5/32")
    for bad in (["not-an-ip"], ["10.0.0.0/33"], [""], ["*"]):
        with pytest.raises(ConfigError):
            from_dict(raw(server__trusted_proxies=bad))


@pytest.mark.parametrize("entry", ["0.0.0.0/0", "::/0", "10.0.0.5/0", "0.0.0.0/0.0.0.0", "::0/0"])
def test_trusted_proxies_reject_networks_that_trust_everyone(entry):
    with pytest.raises(ConfigError, match="trust every address"):
        from_dict(raw(server__trusted_proxies=["172.18.0.2/32", entry]))


@pytest.mark.parametrize("entry", ["0.0.0.0/1", "::/1", "128.0.0.0/1", "0.0.0.0/32", "::1"])
def test_trusted_proxies_keep_wide_but_not_total_networks(entry):
    assert from_dict(raw(server__trusted_proxies=[entry])).trusted_proxies


# ------------------------------------------------------------------ features


def test_event_fields_profile_and_unknown_fields():
    base = ["subject", "location", "time", "all_day", "body", "busy_status", "reminder"]
    assert from_dict(raw(features__event_fields=base)).features is FeatureSet.BASIC
    error(raw(features__event_fields=base + ["attendees"]), "unknown field")
    error(raw(features__event_fields=["subject", "mail"]), "unknown field")
    error(raw(features__event_fields=["subject"]), "supports exactly")
    error(raw(features__event_fields=base + ["categories"]), "supports exactly")
    error(raw(features__event_fields=base + base), "duplicates")


def test_no_builtin_read_only_identity():
    r = raw(features__read_only_owners=[])
    r["users"][0]["calendars"][1]["write"] = "caldav"
    k = from_dict(r)
    assert k.read_only_owners == frozenset() and k.accounts[0].calendar[1].write == "caldav"


def test_read_only_owner_must_not_be_configured_writable():
    r = raw()
    r["users"][0]["calendars"][1]["write"] = "caldav"
    error(r, "read_only_owners")


def test_default_values_without_operator_rules():
    r = raw()
    r.pop("features")
    r["server"].pop("default_timezone")
    k = from_dict(r)
    assert k.read_only_owners == frozenset() and k.protected_property_prefixes == ()
    assert k.default_timezone == "UTC" and k.autodiscover_culture == "en:us"


def test_configured_prefixes_and_fields():
    k = from_dict(raw(features__protected_property_prefixes=["X-EXAMPLE-", "X-OTHER-"], features__protected_event_writable_fields=["subject", "timezone"]))
    assert k.protected_property_prefixes == ("X-EXAMPLE-", "X-OTHER-")
    assert k.protected_writable_uris == frozenset({"item:Subject", "calendar:StartTimeZone", "calendar:EndTimeZone"})


@pytest.mark.parametrize("prefixes", ["X-EXAMPLE-", ["x-example-"], ["X-EXAMPLE"], [""], [1], ["X-EXAMPLE-", "BAD PREFIX-"]])
def test_invalid_prefixes_are_fatal(prefixes):
    with pytest.raises(ConfigError):
        from_dict(raw(features__protected_property_prefixes=prefixes))


@pytest.mark.parametrize("fields", ["subject", [1], ["item:Subject"], ["colour"]])
def test_invalid_writable_fields_are_fatal(fields):
    with pytest.raises(ConfigError):
        from_dict(raw(features__protected_event_writable_fields=fields))


# ------------------------------------------------------------------ users and calendars


def test_no_user_is_fatal():
    r = raw()
    r["users"] = []
    r["auth"]["allowed_users"] = []
    error(r, "no user configured")


def test_duplicate_users_are_fatal():
    r = raw()
    r["users"].append(copy.deepcopy(r["users"][0]))
    error(r, "duplicate user")
    r = raw()
    r["users"].append(copy.deepcopy(r["users"][0]))
    r["users"][1]["username"] = "TEST-USER"
    r["users"][1]["address"] = "other@dav.example.com"
    error(r, "duplicate user")


def test_duplicate_address_is_fatal():
    r = raw()
    r["users"].append(copy.deepcopy(r["users"][0]))
    r["users"][1]["username"] = "other"
    error(r, "addresses must be unique")


def test_allowed_users_without_users_block_is_fatal():
    error(raw(auth__allowed_users=["test-user", "bob"]), "no [[users]] block")
    error(raw(auth__allowed_users=["test-user", "test-user"]), "duplicate entry")


@pytest.mark.parametrize("name", ["", "a b", "../x", "a/b", "a%2Fb", "-x", "x" * 65, "ä"])
def test_invalid_user_names(name):
    r = raw()
    r["users"][0]["username"] = name
    r["auth"]["allowed_users"] = []
    with pytest.raises(ConfigError):
        from_dict(r)


def test_calendar_mode_auto_users_have_no_calendar_list():
    error(raw(users__0__calendar_mode="auto"), "remove the [[users.calendars]]")
    error(raw(users__0__calendar_mode="dynamic"), "must be one of")
    r = raw(users__0__calendar_mode="auto")
    r["users"][0].pop("calendars")
    k = from_dict(r)
    assert k.accounts[0].auto is True and k.accounts[0].calendar == () and k.accounts[0].address == "test-user@dav.example.com"


def test_allowed_users_without_block_need_the_auto_opt_in():
    r = raw(auth__allowed_users=["test-user", "bob"], dynamic_users={"calendar_mode": "auto", "eas_enabled": False})
    k = from_dict(r)
    bob = account_for(k, "bob")
    assert bob is not None and bob.auto and bob.calendar == () and bob.address == "bob@dav.example.com" and bob.eas is False
    assert account_for(k, "test-user").auto is False
    error(raw(auth__allowed_users=["test-user", "bob"], dynamic_users={"calendar_mode": "explicit"}), 'calendar_mode = "auto"')


def test_dynamic_users_defaults_are_the_safe_ones():
    k = from_dict(raw())
    assert (k.dynamic.calendar_mode, k.dynamic.default_calendar, k.dynamic.include_shared, k.dynamic.eas_enabled) == ("explicit", "only", False, False)


@pytest.mark.parametrize("value", ["only", "first-writable", "personal", "my_cal@x"])
def test_dynamic_default_calendar_accepts_rules_and_exact_paths(value):
    assert from_dict(raw(dynamic_users={"default_calendar": value})).dynamic.default_calendar == value


@pytest.mark.parametrize("value", ["", "../x", "a/b", "a b", "%2e%2e"])
def test_dynamic_default_calendar_rejects_anything_else(value):
    error(raw(dynamic_users={"default_calendar": value}), "default_calendar")


def test_user_needs_calendar():
    r = raw()
    r["users"][0].pop("calendars")
    error(r, "[[users.calendars]]")


@pytest.mark.parametrize("defaults", [0, 2])
def test_exactly_one_default_calendar(defaults):
    r = raw()
    for i, cal in enumerate(r["users"][0]["calendars"]):
        cal["default"] = i < defaults
    error(r, "exactly one calendar")


def test_calendar_id_unique_and_not_reserved():
    r = raw()
    r["users"][0]["calendars"][1]["id"] = "test-calendar"
    error(r, "unique")
    r = raw()
    r["users"][0]["calendars"][1]["path"] = "test-calendar"
    error(r, "unique")
    for reserved in ("inbox", "calendar", "root", "tasks"):
        r = raw()
        r["users"][0]["calendars"][1]["id"] = reserved
        error(r, "reserved")


@pytest.mark.parametrize("identifier", ["Large", "with empty", "a" * 41, "", "ä"])
def test_invalid_calendar_ids(identifier):
    r = raw()
    r["users"][0]["calendars"][1]["id"] = identifier
    with pytest.raises(ConfigError):
        from_dict(r)


NESTED_PATHS = ["x%252F..%252Fbob", "x%252e%252e%252fsecret", "a%255Cb", "a%2525", "a%00b", "a%0Ab", "a%7F", "a%C3"]
ENCODED_PATHS = ["x%2F..%2F..%2Fbob%2Fcalendar", "a%2fb", "a%5Cb", "a%5cb", "x%2E%2E", "x%2ex", "a..%2F", "..", ".", "a\n", "%2e%2e", *NESTED_PATHS]


@pytest.mark.parametrize("path", ENCODED_PATHS)
def test_calendar_paths_with_encoded_separators_or_dots_are_rejected(path):
    r = raw()
    r["users"][0]["calendars"][1]["path"] = path
    error(r, "path")


@pytest.mark.parametrize("path", ["my%40cal", "a%20b", "%C3%A4", "caf%C3%A9-cal", "personal_shared_by_bob", "a.b-c_d+e@f", "a..b"])
def test_other_calendar_paths_stay_valid(path):
    r = raw()
    r["users"][0]["calendars"][1]["path"] = path
    assert from_dict(r).accounts[0].calendar[1].uri == path


@pytest.mark.parametrize("path", ENCODED_PATHS)
def test_dynamic_default_calendar_rejects_encoded_separators_too(path):
    error(raw(dynamic_users={"default_calendar": path}), "default_calendar")


def test_invalid_path_and_write_mode():
    r = raw()
    r["users"][0]["calendars"][1]["path"] = "../x"
    error(r, "path")
    r = raw()
    r["users"][0]["calendars"][1]["write"] = "maybe"
    error(r, "write")
    r = raw()
    r["users"][0]["calendars"][1]["write"] = "nextcloud"
    error(r, "write")


def test_missing_required_fields():
    r = raw()
    del r["users"][0]["calendars"][0]["path"]
    error(r, "path")
    r = raw()
    del r["users"][0]["username"]
    error(r, "username")
    for section, key in (("server", "public_url"), ("caldav", "base_url")):
        r = raw()
        del r[section][key]
        error(r, key)


def test_address_template():
    r = raw(auth__address_template="{user}@mail.example.org")
    del r["users"][0]["address"]
    assert from_dict(r).accounts[0].address == "test-user@mail.example.org"
    for bad in ("fixed@example.org", "{user}{user}@x.org", "{host}@x.org", "{user}@{domain}"):
        with pytest.raises(ConfigError):
            from_dict(raw(auth__address_template=bad))


def test_account_for_name_and_address_case_insensitive():
    k = from_dict(raw())
    assert account_for(k, "TEST-USER").name == "test-user"
    assert account_for(k, " TEST-USER@dav.example.com ").name == "test-user"
    assert account_for(k, "alice") is None
    assert account_for(k, "") is None


# ------------------------------------------------------------------ authentication modes


PATTERN = {"auth": {"mode": "pattern", "username_pattern": "^[a-z][a-z0-9._-]{0,63}$"}, "dynamic_users": {"calendar_mode": "auto"}}


def pattern_raw(**paths):
    r = raw()
    for section, values in PATTERN.items():
        r.setdefault(section, {}).update(values)
    for path, value in paths.items():
        node = r
        parts = path.split("__")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return r


def test_pattern_mode_is_valid_and_starts():
    k = from_dict(pattern_raw())
    assert k.auth_mode == "pattern" and k.username_pattern.startswith("^") and k.dynamic.calendar_mode == "auto"
    assert check_start(k) is None and check_start(from_dict(raw())) is None


def test_pattern_mode_needs_the_auto_calendar_opt_in():
    error(raw(auth__mode="pattern", auth__username_pattern="^[a-z]+$"), "requires [dynamic_users] calendar_mode")


def test_dynamic_account_requires_a_full_match_and_a_plain_name():
    k = from_dict(pattern_raw())
    a = dynamic_account(k, "bob")
    assert a is not None and (a.name, a.address, a.auto, a.eas, a.calendar) == ("bob", "bob@dav.example.com", True, False, ())
    assert dynamic_account(k, "  carol ").name == "carol"
    assert dynamic_account(k, "bob@dav.example.com").name == "bob"
    assert dynamic_account(k, "BOB@DAV.EXAMPLE.COM") is None
    for bad in ("", "Bob", "1bob", "bob/..", "bob%2f", "b o b", "bob@other.example.com", "x" * 65, "https://evil.example", "../etc", "bob\nx"):
        assert dynamic_account(k, bad) is None, bad
    assert dynamic_account(from_dict(raw()), "bob") is None


def test_dynamic_account_is_never_derived_from_a_partial_match():
    k = from_dict(pattern_raw(auth__username_pattern="^[a-z]+$"))
    assert dynamic_account(k, "abc").name == "abc"
    assert dynamic_account(k, "abc1") is None
    assert dynamic_account(k, "abc\n").name == "abc"


def test_dynamic_eas_needs_the_global_switch_and_the_acknowledgement():
    base = {"dynamic_users": {"calendar_mode": "auto", "eas_enabled": True}}
    error(pattern_raw(dynamic_users=base["dynamic_users"]), "eas_enabled = true but protocols.eas.enabled is false")
    k = from_dict(pattern_raw(dynamic_users=base["dynamic_users"], protocols__eas={"enabled": True, "acknowledge_mobile_cloud_processing": True}))
    assert k.eas.enabled and dynamic_account(k, "bob").eas is True
    error(pattern_raw(dynamic_users=base["dynamic_users"], protocols__eas={"enabled": True}), "acknowledge_mobile_cloud_processing")


@pytest.mark.parametrize("pattern", ["", "[a-z]+", "^[a-z]+", "[a-z]+$", "^(unclosed$"])
def test_pattern_mode_needs_anchored_valid_pattern(pattern):
    with pytest.raises(ConfigError):
        from_dict(raw(auth__mode="pattern", auth__username_pattern=pattern))


def test_dynamic_users_is_validated():
    r = raw()
    r["dynamic_users"] = {"calendar_mode": "auto", "default_calendar": "only", "include_shared": False, "eas_enabled": False}
    from_dict(r)
    r["dynamic_users"]["include_shared"] = "no"
    with pytest.raises(ConfigError):
        from_dict(r)
    error(raw(dynamic_users={"calendar": "auto"}), "unknown key")


# ------------------------------------------------------------------ EAS: double opt-in and limits


def eas_raw(**eas):
    r = raw()
    r["users"][0]["eas_enabled"] = True
    r["protocols"]["eas"] = {"enabled": True, "acknowledge_mobile_cloud_processing": True, **eas}
    return r


def test_eas_needs_all_three_switches():
    k = from_dict(eas_raw())
    assert k.eas.enabled and k.eas.acknowledged and k.accounts[0].eas
    error(eas_raw(acknowledge_mobile_cloud_processing=False), "acknowledge_mobile_cloud_processing")
    r = eas_raw()
    del r["protocols"]["eas"]["acknowledge_mobile_cloud_processing"]
    error(r, "acknowledge_mobile_cloud_processing")
    r = eas_raw()
    r["users"][0]["eas_enabled"] = False
    error(r, "no user has eas_enabled")
    r = raw()
    r["users"][0]["eas_enabled"] = True
    error(r, "protocols.eas.enabled is false")
    error(raw(protocols__eas={"enabled": False, "eas_enabled": True}), "unknown key")


def test_eas_acknowledgement_alone_enables_nothing():
    k = from_dict(raw(protocols__eas={"acknowledge_mobile_cloud_processing": True}))
    assert k.eas.enabled is False and k.eas.acknowledged is True


def test_at_least_one_protocol():
    error(raw(protocols__ews={"enabled": False}), "at least one")
    k = from_dict(eas_raw() | {"protocols": {"ews": {"enabled": False}, "eas": eas_raw()["protocols"]["eas"]}})
    assert k.ews_enabled is False and k.eas.enabled is True


@pytest.mark.parametrize(
    "bad",
    [
        {"enabled": "false"},
        {"enabled": 1},
        {"window_max": True},
        {"window_max": "100"},
        {"delete_brake_minimum": 1.5},
        {"versions": "14.1"},
        {"versions": ["14.1", "16.0"]},
        {"versions": ["16.1"]},
        {"versions": []},
    ],
)
def test_eas_types_and_versions(bad):
    with pytest.raises(ConfigError):
        from_dict(eas_raw(**bad))


@pytest.mark.parametrize(
    "key,value",
    [
        ("window_max", 0),
        ("window_max", 513),
        ("heartbeat_min_seconds", 59),
        ("heartbeat_max_seconds", 541),
        ("device_limit", 0),
        ("device_limit", 21),
        ("delete_brake_minimum", 0),
        ("delete_brake_minimum", 1001),
        ("wait_max_minutes", 10),
        ("ping_limit_per_user", 9),
        ("device_expiry_days", 0),
        ("request_max_seconds", 4),
        ("request_body_max_bytes", 1024 * 1024 + 1),
        ("response_max_bytes", 100),
        ("body_max_chars", 10),
    ],
)
def test_eas_ranges(key, value):
    with pytest.raises(ConfigError):
        from_dict(eas_raw(**{key: value}))


def test_eas_heartbeat_order_and_values():
    with pytest.raises(ConfigError):
        from_dict(eas_raw(heartbeat_min_seconds=300, heartbeat_max_seconds=120))
    k = from_dict(eas_raw(heartbeat_min_seconds=120, heartbeat_max_seconds=300, window_max=50, device_limit=3, delete_brake_minimum=7))
    assert (k.eas.heartbeat_min_seconds, k.eas.heartbeat_max_seconds, k.eas.window_max, k.eas.device_limit, k.eas.delete_brake_minimum) == (120, 300, 50, 3, 7)


# ------------------------------------------------------------------ files


def test_load_reads_file_and_reports_errors_in_english(tmp_path):
    from davbridge.toml_writer import dumps

    path = tmp_path / "c.toml"
    path.write_text(dumps(raw()))
    assert load(path, {}).accounts[0].name == "test-user"
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "missing.toml", {})
    path.write_text("config_version = [")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load(path, {})


def test_example_config_is_valid():
    from pathlib import Path

    example = Path(__file__).resolve().parents[3] / "config" / "davbridge.example.toml"
    k = load(example, {})
    assert k.accounts[0].name == "alice" and k.trusted_proxies == ("172.18.0.2/32",) and k.eas.enabled is False
    assert k.stream_max_seconds == 1800 and k.event_fields[0] == "subject"


@pytest.mark.parametrize("seconds", [0, 59, 1801])
def test_stream_max_seconds_is_bounded(seconds):
    with pytest.raises(ConfigError):
        from_dict(raw(protocols__ews__stream_max_seconds=seconds))
