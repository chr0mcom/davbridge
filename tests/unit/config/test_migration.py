"""Legacy configuration converter: key mapping, stage handling, safety of the file operations, golden file."""

import copy
import os
import stat
import tomllib
from pathlib import Path

import pytest

from davbridge import config_migration as m
from davbridge.config import ConfigError, from_dict

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "config"
LEGACY = FIXTURES / "legacy-neutral.toml"
EXPECTED = FIXTURES / "legacy-neutral.expected.toml"


def old():
    return tomllib.loads(LEGACY.read_text())


def test_golden_file(tmp_path):
    target = tmp_path / "new.toml"
    m.migrate_files(LEGACY, target, env={})
    assert target.read_text() == EXPECTED.read_text()


def test_golden_file_is_loadable_with_the_production_validator_after_acknowledgement():
    doc = tomllib.loads(EXPECTED.read_text())
    with pytest.raises(ConfigError, match="acknowledge_mobile_cloud_processing"):
        from_dict(doc)
    doc["protocols"]["eas"]["acknowledge_mobile_cloud_processing"] = True
    k = from_dict(doc)
    assert [u.name for u in k.accounts] == ["alice", "bob"] and k.eas.enabled and k.eas.window_max == 50


def test_every_old_key_is_mapped():
    r = m.migrate_legacy(old())
    d = r.document
    assert d["config_version"] == 1
    assert d["server"]["public_url"] == "https://dav.example.com" and d["server"]["default_timezone"] == "Europe/Berlin"
    assert d["server"]["autodiscover_culture"] == "en:us" and d["server"]["trusted_proxies"] == []
    assert d["caldav"]["base_url"] == "https://cloud.example.com/remote.php/dav"
    assert d["auth"] == {"mode": "allowlist", "positive_cache_seconds": 120, "failure_limit": 5, "initial_lock_seconds": 600, "allowed_users": ["alice", "bob"]}
    assert d["protocols"]["ews"] == {"enabled": True, "stream_max_seconds": 540, "event_detail": "both"}
    assert d["protocols"]["eas"] == {
        "enabled": True,
        "acknowledge_mobile_cloud_processing": False,
        "versions": ["14.1"],
        "window_max": 50,
        "heartbeat_min_seconds": 120,
        "heartbeat_max_seconds": 480,
        "device_limit": 4,
        "delete_brake_minimum": 8,
        "body_max_chars": 20000,
    }
    assert d["features"]["read_only_owners"] == ["read-only-owner"] and d["features"]["protected_property_prefixes"] == ["X-EXAMPLE-PROTECTED-"]
    assert d["features"]["protected_event_writable_fields"] == ["subject", "location", "start", "end", "all_day", "timezone"]
    u = d["users"][0]
    assert (u["username"], u["address"], u["display_name"], u["eas_enabled"], u["calendar_mode"]) == ("alice", "alice@dav.example.com", "Alice Example", True, "explicit")
    assert u["calendars"][0] == {"path": "personal", "id": "primary", "display_name": "Primary Calendar", "default": True, "write": "caldav", "owner": "alice"}
    assert u["calendars"][1]["write"] == "never" and u["calendars"][1]["default"] is False and u["calendars"][1]["owner"] == "read-only-owner"


@pytest.mark.parametrize("old_value,new", [("ordner", "folder"), ("element", "item"), ("beide", "both")])
def test_event_detail(old_value, new):
    r = old()
    r["ereignis_detail"] = old_value
    assert m.migrate_legacy(r).document["protocols"]["ews"]["event_detail"] == new


def test_only_set_values_are_adopted_and_operator_values_stay():
    r = old()
    for k in ("zugang_pruef_s", "sperre_fehlversuche", "sperre_sekunden", "default_timezone", "autodiscover_culture", "ereignis_detail", "stream_max_s"):
        r.pop(k, None)
    r["stream_max_s"] = 900
    d = m.migrate_legacy(r).document
    assert d["auth"] == {"mode": "allowlist", "allowed_users": ["alice", "bob"]}
    assert "default_timezone" not in d["server"] and d["protocols"]["ews"] == {"enabled": True, "stream_max_seconds": 900}


def test_old_default_stream_duration_is_preserved():
    r = old()
    r.pop("stream_max_s", None)
    res = m.migrate_legacy(r)
    assert res.document["protocols"]["ews"]["stream_max_seconds"] == 540
    assert any("540" in n for n in res.notes)


def test_obsolete_switches_are_reported_not_adopted():
    res = m.migrate_legacy(old())
    text = "\n".join(res.obsolete)
    for name in ("messmodus_tolerant", "delete_ohne_changekey", "poll_aktiv_s", "stufe = M2", "test", "messung"):
        assert name in text
    dumped = str(res.document)
    assert "messmodus" not in dumped and "delete_ohne" not in dumped and "stufe" not in dumped


@pytest.mark.parametrize("stage", ["M0", "M1", "M3"])
def test_other_stages_abort_without_feature_profile(stage):
    r = old()
    r["stufe"] = stage
    with pytest.raises(m.MigrationError, match=f"{stage}.*--feature-profile"):
        m.migrate_legacy(r)
    assert m.migrate_legacy(r, feature_profile="default").document["features"]["event_fields"][-1] == "sensitivity"
    basic = m.migrate_legacy(r, feature_profile="basic").document["features"]["event_fields"]
    assert "categories" not in basic and "sensitivity" not in basic
    with pytest.raises(m.MigrationError, match="unknown feature profile"):
        m.migrate_legacy(r, feature_profile="m2")


def test_missing_or_unknown_stage():
    r = old()
    del r["stufe"]
    with pytest.raises(m.MigrationError, match="stufe"):
        m.migrate_legacy(r)
    r["stufe"] = "M9"
    with pytest.raises(m.MigrationError, match="stufe"):
        m.migrate_legacy(r)


def test_unknown_old_keys_are_not_guessed():
    for path in ("neu", "richtlinie.x", "eas.x", "konto.0.x", "konto.0.kalender.0.x"):
        r = copy.deepcopy(old())
        node = r
        parts = path.split(".")
        for t in parts[:-1]:
            node = node[int(t)] if t.isdigit() else node[t]
        node[parts[-1]] = 1
        with pytest.raises(m.MigrationError, match="unknown legacy key"):
            m.migrate_legacy(r)


def test_eas_acknowledgement_is_never_set():
    res = m.migrate_legacy(old())
    assert res.document["protocols"]["eas"]["acknowledge_mobile_cloud_processing"] is False
    assert any("acknowledge_mobile_cloud_processing" in a for a in res.actions)
    r = old()
    r["eas"]["aktiv"] = False
    r["konto"][0]["eas"] = False
    res = m.migrate_legacy(r)
    assert res.document["protocols"]["eas"]["acknowledge_mobile_cloud_processing"] is False
    assert not any("acknowledge" in a for a in res.actions)


def test_eas_without_section_stays_off():
    r = old()
    del r["eas"]
    for k in r["konto"]:
        k["eas"] = False
    assert "eas" not in m.migrate_legacy(r).document["protocols"]


def test_unmappable_write_fields_are_reported():
    r = old()
    r["richtlinie"]["protected_event_writable_fields"] = ["item:Subject", "calendar:StartTimeZone", "item:Missing"]
    res = m.migrate_legacy(r)
    assert res.document["features"]["protected_event_writable_fields"] == ["subject"]
    assert any("item:Missing" in n and "calendar:StartTimeZone" in n for n in res.notes)


# ------------------------------------------------------------------ file operations


def test_target_file_mode_0600_and_never_overwrite(tmp_path):
    target = tmp_path / "davbridge.toml"
    m.migrate_files(LEGACY, target, env={})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    content = target.read_text()
    with pytest.raises(m.MigrationError, match="refusing to overwrite"):
        m.migrate_files(LEGACY, target, env={})
    assert target.read_text() == content
    assert [p.name for p in tmp_path.iterdir()] == ["davbridge.toml"]


def test_does_not_follow_symlinks(tmp_path):
    source_link = tmp_path / "legacy-link.toml"
    source_link.symlink_to(LEGACY)
    with pytest.raises(m.MigrationError, match="symbolic links"):
        m.migrate_files(source_link, tmp_path / "out.toml", env={})
    secret = tmp_path / "secret.txt"
    secret.write_text("keep")
    target_link = tmp_path / "target-link.toml"
    target_link.symlink_to(secret)
    with pytest.raises(m.MigrationError, match="refusing to overwrite"):
        m.migrate_files(LEGACY, target_link, env={})
    assert secret.read_text() == "keep"
    target_dangling = tmp_path / "dangling.toml"
    target_dangling.symlink_to(tmp_path / "nowhere")
    with pytest.raises(m.MigrationError, match="refusing to overwrite"):
        m.migrate_files(LEGACY, target_dangling, env={})
    assert not (tmp_path / "nowhere").exists()


def test_error_cases_write_nothing(tmp_path):
    target = tmp_path / "out.toml"
    with pytest.raises(m.MigrationError, match="not found"):
        m.migrate_files(tmp_path / "missing.toml", target, env={})
    broken = tmp_path / "broken.toml"
    broken.write_text("stufe = [")
    with pytest.raises(m.MigrationError, match="not valid TOML"):
        m.migrate_files(broken, target, env={})
    with pytest.raises(m.MigrationError, match="not a regular file"):
        m.migrate_files(tmp_path, target, env={})
    http = tmp_path / "http.toml"
    http.write_text(LEGACY.read_text().replace("https://cloud.example.com", "http://cloud.example.com"))
    with pytest.raises(m.MigrationError, match="not valid"):
        m.migrate_files(http, target, env={})
    with pytest.raises(m.MigrationError, match="target directory"):
        m.migrate_files(LEGACY, tmp_path / "nodir" / "out.toml", env={})
    assert not target.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["broken.toml", "http.toml"]


@pytest.mark.parametrize("path", ["x%2F..%2F..%2Fbob%2Fcalendar", "a%5cb", "x%2Ex", "x%252F..%252Fbob", "x%252e%252e%252fsecret"])
def test_migration_refuses_calendar_paths_that_could_leave_the_segment(tmp_path, path):
    source = tmp_path / "legacy.toml"
    source.write_text(LEGACY.read_text().replace('uri = "personal"', f'uri = "{path}"', 1))
    with pytest.raises(m.MigrationError, match="not valid"):
        m.migrate_files(source, tmp_path / "out.toml", env={})
    assert not (tmp_path / "out.toml").exists()


def test_source_is_never_changed_and_password_files_are_not_read(tmp_path, monkeypatch):
    copy = tmp_path / "legacy.toml"
    copy.write_text(LEGACY.read_text())
    before = copy.read_bytes()
    opened = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: (opened.append(str(p)), real_open(p, *a, **k))[1])
    m.migrate_files(copy, tmp_path / "new.toml", env={})
    assert copy.read_bytes() == before
    assert not any("pass" in g or "secret" in g for g in opened)


def test_report_is_english_and_contains_actions(tmp_path):
    res = m.migrate_files(LEGACY, tmp_path / "n.toml", env={})
    text = m.format_report(res, Path("old.toml"), Path("n.toml"))
    assert text.isascii() and "Action required" in text and "Not carried over" in text and "users: 2, calendars: 3" in text
