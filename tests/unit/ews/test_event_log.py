import json
import os
import time

import pytest

from davbridge.event_log import ALLOWED_KEYS, EventLog, FileStorage


class FakeClock:
    def __init__(self, t=1_793_000_000.0):
        self.t = t

    def now(self):
        return self.t


@pytest.fixture
def construction(tmp_path):
    clock = FakeClock()
    storage = FileStorage(tmp_path / "logs", now=clock.now)
    p = EventLog(tmp_path / "logs", clock, storage)
    yield p, storage, clock, tmp_path / "logs"
    storage.close()


def lines(storage, dir):
    storage.wait_empty()
    out = []
    for d in sorted(dir.glob("davbridge-*.jsonl")):
        out += [json.loads(z) for z in d.read_text(encoding="utf-8").splitlines()]
    return out


def test_request_writes_json_line(construction):
    p, storage, clock, dir = construction
    p.request("GetFolder", "test-user", ["NoError", "ErrorFolderNotFound"], 2, 15, folder="test-calendar", element="0123456789ab", request_id="r-1")
    (z,) = lines(storage, dir)
    assert z["event"] == "request" and z["operation"] == "GetFolder" and z["codes"] == ["NoError", "ErrorFolderNotFound"]
    assert z["user"] == "test-user" and z["folder"] == "test-calendar" and z["request_id"] == "r-1"
    assert z["count"] == 2 and z["duration_ms"] == 15 and z["item"] == "0123456789ab" and z["time"].endswith("+00:00")
    assert list(dir.glob("davbridge-*.jsonl"))[0].name == "davbridge-2026-10-26.jsonl"
    assert oct(list(dir.glob("davbridge-*.jsonl"))[0].stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("name", ["alice@example.org", "bob+calendar@example.org", "test-user", "a.b_c-d"])
def test_user_names_with_at_and_plus_are_logged(construction, name):
    p, storage, clock, dir = construction
    p.request("GetFolder", name, ["NoError"], 1, 3)
    z = lines(storage, dir)[0]
    assert z["user"] == name and p.discarded == 0


def test_unknown_keys_and_invalid_values_are_discarded_and_counted(construction):
    p, storage, clock, dir = construction
    p.event("test", subject="Dentist private", operation="Get Folder", element="Appointment-Title", account="Alice X", count=3, value=None)
    (z,) = lines(storage, dir)
    assert set(z) == {"time", "event", "count"}
    assert p.discarded == 4


def test_missing_values_are_omitted_without_counting(construction):
    p, storage, clock, dir = construction
    p.request("GetFolder", "test-user", ("NoError",), 1, 3)
    (z,) = lines(storage, dir)
    assert "folder" not in z and "item" not in z and p.discarded == 0


def test_only_allowed_keys_in_the_allowlist():
    for forbidden in ("betreff", "subject", "passwort", "authorization", "body", "ort", "ip", "attendees", "uid", "str_e"):
        assert forbidden not in ALLOWED_KEYS


def test_list_values_are_sanitized(construction):
    p, storage, clock, dir = construction
    p.event("x", codes=["NoError", "with space", "ErrorX"] + ["A"] * 40)
    (z,) = lines(storage, dir)
    assert "with space" not in z["codes"] and len(z["codes"]) <= 20


def test_error_logs_only_the_type_name(construction):
    p, storage, clock, dir = construction
    try:
        raise ValueError("https://cloud.example.com/remote.php/dav/calendars/x?password=secret-123 Dentist")
    except ValueError as e:
        p.error("exception", e, operation="UpdateItem")
    storage.wait_empty()
    text = "".join(d.read_text() for d in dir.glob("davbridge-*.jsonl"))
    assert "secret-123" not in text and "Dentist" not in text and "https" not in text
    (z,) = lines(storage, dir)
    assert z["error_type"] == "ValueError" and z["operation"] == "UpdateItem"


def test_log_scan_for_test_titles_and_secrets(construction, tmp_path):
    p, storage, clock, dir = construction
    secrets = ["Test title Dentist private", "hunter2 in the subject", "mail (example.org)", "Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ=="]
    for s in secrets:
        p.request(s, s, [s], 1, 1, folder=s, element=s, request_id=s)
        p.event("x", **{"reason": s, "status": s, "caldav_kind": s})
    p.request("Sync", "test-user", ["NoError"], 1, 1)
    text = "".join(d.read_text() for d in dir.glob("davbridge-*.jsonl")) if lines(storage, dir) else ""
    for s in secrets:
        assert s not in text


def test_storage_does_not_write_outside_root(tmp_path):
    a = FileStorage(tmp_path / "logs")
    try:
        a.write(tmp_path / "aussen.jsonl", "x")
        a.write(tmp_path / "logs" / ".." / "aussen2.jsonl", "x")
        a.wait_empty()
        assert not (tmp_path / "aussen.jsonl").exists() and not (tmp_path / "aussen2.jsonl").exists()
        assert a.error == 2
    finally:
        a.close()


def test_once_writes_only_the_first_time(tmp_path):
    a = FileStorage(tmp_path / "logs")
    try:
        a.write(tmp_path / "logs" / "skeletons" / "ab.json", "erste", one_time=True)
        a.write(tmp_path / "logs" / "skeletons" / "ab.json", "zweite", one_time=True)
        a.wait_empty()
        assert (tmp_path / "logs" / "skeletons" / "ab.json").read_text() == "erste\n"
        assert not list((tmp_path / "logs" / "skeletons").glob("*.tmp"))
    finally:
        a.close()


def test_retention_30_days(tmp_path):
    dir = tmp_path / "logs"
    dir.mkdir()
    old, new, foreign = dir / "davbridge-2026-01-01.jsonl", dir / "davbridge-2026-10-25.jsonl", dir / "notiz.txt"
    for d in (old, new, foreign):
        d.write_text("x\n")
    now = 1_793_000_000.0
    os.utime(old, (now - 31 * 86400, now - 31 * 86400))
    os.utime(new, (now - 29 * 86400, now - 29 * 86400))
    os.utime(foreign, (now - 99 * 86400, now - 99 * 86400))
    a = FileStorage(dir, now=lambda: now)
    try:
        a.write(dir / "davbridge-2026-10-26.jsonl", "{}")
        a.wait_empty()
    finally:
        a.close()
    assert not old.exists() and new.exists() and foreign.exists()


def test_writing_does_not_block_and_counts_losses(tmp_path):
    a = FileStorage(tmp_path / "logs", size=2)
    try:
        import threading

        keep = threading.Event()
        original = a._write
        a._write = lambda *x: keep.wait(5)
        a.write(tmp_path / "logs" / "a.jsonl", "1")  # is taken by the thread and holds on
        time.sleep(0.2)
        t0 = time.monotonic()
        for i in range(10):
            a.write(tmp_path / "logs" / "a.jsonl", str(i))
        assert time.monotonic() - t0 < 0.5
        assert a.lost >= 8
        keep.set()
        a._write = original
    finally:
        a.close()


def test_storage_errors_do_not_disturb_the_caller(tmp_path):
    a = FileStorage(tmp_path / "logs")
    try:
        (tmp_path / "logs" / "file").write_text("x")
        a.write(tmp_path / "logs" / "file" / "impossible.jsonl", "x")  # the parent is a file
        a.write(tmp_path / "logs" / "ok.jsonl", "y")
        a.wait_empty()
        assert a.error == 1 and (tmp_path / "logs" / "ok.jsonl").read_text() == "y\n"
    finally:
        a.close()


def test_day_boundary_limits_the_log_volume(construction, monkeypatch):
    p, storage, clock, dir = construction
    monkeypatch.setattr("davbridge.event_log.MAX_TAG_BYTES", 2000)
    for _ in range(200):
        p.event("raw_request", operation="ews", status=200)
    storage.wait_empty()
    total = sum(f.stat().st_size for f in dir.rglob("*.jsonl"))
    assert total <= 2100 and storage.lost > 0
    clock.t += 86400
    p.event("raw_request", operation="ews", status=200)
    storage.wait_empty()
    assert sum(f.stat().st_size for f in dir.rglob("*.jsonl")) > total


def test_public_field_names_are_english_and_closed():
    from davbridge.event_log import ALLOWED_FIELDS, FIELD_NAMES

    assert ALLOWED_FIELDS == frozenset(FIELD_NAMES.values())
    for name in ALLOWED_FIELDS:
        assert name.isascii() and name == name.lower(), name
    for removed_german_name in ("zeit", "konto", "anzahl", "dauer_ms", "grund", "befehl", "geraet", "anfrage_id", "ordner", "element"):
        assert removed_german_name not in ALLOWED_FIELDS


def test_event_names_and_reasons_are_logged_as_given(construction):
    p, storage, clock, dir = construction
    p.event("login_rejected", reason="wrong-password")
    p.event("login_rejected", reason="unknown-user")
    p.event("change_rejected", reason="delete-brake", folder="test-calendar", device="0123456789ab")
    p.event("folder_created", device="0123456789ab", value=12)
    z = lines(storage, dir)
    assert [(e["event"], e.get("reason")) for e in z] == [("login_rejected", "wrong-password"), ("login_rejected", "unknown-user"), ("change_rejected", "delete-brake"), ("folder_created", None)]
    assert z[2]["device"] == "0123456789ab" and z[3]["value"] == 12


def test_log_level_filters_below_the_level(tmp_path):
    clock = FakeClock()
    storage = FileStorage(tmp_path / "logs", now=clock.now)
    p = EventLog(tmp_path / "logs", clock, storage, level="warning")
    p.request("GetFolder", "test-user", ["NoError"], 1, 1)
    p.event("login_rejected", reason="wrong-password")
    p.event("folder_access", folder="x")
    p.error("operation_exception", ValueError("secret"))
    assert [z["event"] for z in lines(storage, tmp_path / "logs")] == ["login_rejected", "operation_exception"]
    storage.close()


def test_stdout_is_target_without_files(tmp_path, capsys):
    clock = FakeClock()
    storage = FileStorage(None, now=clock.now)
    p = EventLog(None, clock, storage, stdout=True)
    p.request("GetFolder", "test-user", ["NoError"], 1, 1)
    storage.wait_empty()
    storage.close()
    output = capsys.readouterr().out.strip().splitlines()
    assert len(output) == 1 and json.loads(output[0])["event"] == "request"
    assert list(tmp_path.iterdir()) == []
