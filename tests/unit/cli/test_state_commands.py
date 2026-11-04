"""backup, migrate --check and rotate-key: consistent copies, no side effects on check, consequences of a key rotation."""

import asyncio
import errno
import json
import os
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest
from tests.conftest import TEST_CONFIG

from davbridge import cli
from davbridge.clock import SystemClock
from davbridge.config import read_key
from davbridge.store import SCHEMA, SCHEMA_EAS, SCHEMA_VERSION, Store
from davbridge.toml_writer import dumps


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


def run(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


async def populate(environment) -> Store:
    store = await Store.open(environment / "state" / "davbridge.sqlite", SystemClock())
    await store.new_state("test-user", "test-calendar", "token", {"a.ics": '"1"'}, {})
    assert await store.eas_device_see("test-user", "aaaaaaaaaaaa", "iPhone", "Outlook-iOS/2.0", 6) == "new"
    await store.dedup_remember("test-user", b"k" * 32, "test-calendar", "x.ics")
    await store.config_set("test-user", "calendar", "OWA.UserOptions", b"<x/>", new=True)
    await store.event("test-user", "delivered", folder="test-calendar", element="e", etag_hash="h", value=1)
    for _ in range(10):
        await store.lock_failed_attempt("203.0.113.7")
    return store


def mode(path) -> int:
    return path.stat().st_mode & 0o777


# ------------------------------------------------------------------ backup


def test_backup_is_consistent_while_the_service_holds_the_database_in_wal_mode(environment, capsys):
    target = environment / "backup.sqlite"

    async def scenario():
        store = await populate(environment)
        try:
            assert (environment / "state" / "davbridge.sqlite-wal").exists()
            return await asyncio.to_thread(run, ["backup", "--output", str(target)], capsys)
        finally:
            await store.close()

    code, out, _ = asyncio.run(scenario())
    assert code == 0 and "Wrote" in out and "sha256" in out and "does not contain hmac.key" in out
    assert mode(target) == 0o600
    assert not (environment / "backup.sqlite-wal").exists() and not (environment / "backup.sqlite-shm").exists()
    db = sqlite3.connect(target)
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert db.execute("SELECT count(*) FROM sync_state").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM eas_device").fetchone()[0] == 1
    assert db.execute("SELECT failures FROM lockout").fetchone() is not None
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    db.close()
    assert sorted(p.name for p in environment.iterdir() if p.name.startswith(".")) == []


def test_backup_never_overwrites_without_force_and_replaces_atomically_with_it(environment, capsys):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())
    target = environment / "b.sqlite"
    target.write_text("old")
    code, _, err = run(["backup", "--output", str(target)], capsys)
    assert code == 1 and "refusing to overwrite" in err and target.read_text() == "old"
    code, _, _ = run(["backup", "--output", str(target), "--force"], capsys)
    assert code == 0 and sqlite3.connect(target).execute("PRAGMA integrity_check").fetchone()[0] == "ok" and mode(target) == 0o600
    assert not [p for p in environment.iterdir() if ".tmp-" in p.name]


def test_backup_creates_one_missing_directory_level_with_mode_0700(environment, capsys):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())
    target = environment / "backup" / "davbridge-2026-11-04.tar"
    code, _, _ = run(["backup", "--output", str(target)], capsys)
    assert code == 0 and target.is_file() and mode(target.parent) == 0o700
    again = environment / "backup" / "second.sqlite"
    assert run(["backup", "--output", str(again)], capsys)[0] == 0 and again.is_file()


def test_backup_does_not_create_a_chain_of_directories_or_replace_a_file_with_a_directory(environment, capsys):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())
    assert run(["backup", "--output", str(environment / "a" / "b" / "x.tar")], capsys)[0] == 1
    assert not (environment / "a").exists()
    (environment / "plain").write_text("file")
    code, _, err = run(["backup", "--output", str(environment / "plain" / "x.tar")], capsys)
    assert code == 1 and "does not exist" in err


@pytest.mark.parametrize("error", [errno.EPERM, errno.ENOTSUP, errno.EXDEV, errno.EOPNOTSUPP])
def test_backup_falls_back_to_rename_without_hard_links(environment, capsys, monkeypatch, error):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())

    def no_links(src, dst, *a, **k):
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(os, "link", no_links)
    target = environment / "b.sqlite"
    code, _, _ = run(["backup", "--output", str(target)], capsys)
    assert code == 0 and sqlite3.connect(target).execute("PRAGMA integrity_check").fetchone()[0] == "ok" and mode(target) == 0o600
    assert not [p for p in environment.iterdir() if ".tmp-" in p.name]
    code, _, err = run(["backup", "--output", str(target)], capsys)
    assert code == 1 and "refusing to overwrite" in err


def test_backup_fallback_does_not_hide_other_link_errors(environment, capsys, monkeypatch):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())

    def full(src, dst, *a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "link", full)
    code, _, err = run(["backup", "--output", str(environment / "b.sqlite")], capsys)
    assert code == 1 and "No space left" in err and not (environment / "b.sqlite").exists()


def test_backup_error_cases(environment, capsys):
    code, _, err = run(["backup", "--output", str(environment / "x.sqlite")], capsys)
    assert code == 1 and "no state database" in err
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())
    code, _, err = run(["backup", "--output", str(environment / "missing-dir" / "deeper" / "x.sqlite")], capsys)
    assert code == 1 and "does not exist" in err and not (environment / "missing-dir").exists()
    (environment / "state" / "davbridge.sqlite").write_bytes(b"garbage" * 1000)
    code, _, err = run(["backup", "--output", str(environment / "y.sqlite")], capsys)
    assert code == 1 and "cannot back up" in err and not (environment / "y.sqlite").exists()
    assert not [p for p in environment.iterdir() if ".tmp-" in p.name]


def test_tar_backup_bundles_database_key_config_and_checksums(environment, capsys):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())
    target = environment / "davbridge-backup.tar"
    code, out, _ = run(["backup", "--output", str(target)], capsys)
    assert code == 0 and "contains hmac.key" in out and mode(target) == 0o600
    with tarfile.open(target) as archive:
        names = archive.getnames()
        assert sorted(names) == ["davbridge.sqlite", "davbridge.toml", "hmac.key", "manifest.json"]
        assert all(m.mode == 0o600 for m in archive.getmembers())
        data = {n: archive.extractfile(n).read() for n in names}
    manifest = json.loads(data["manifest.json"])
    import hashlib

    assert manifest["format"] == 1 and manifest["schema_version"] == SCHEMA_VERSION
    for name, info in manifest["files"].items():
        assert hashlib.sha256(data[name]).hexdigest() == info["sha256"] and len(data[name]) == info["bytes"]
    assert data["hmac.key"] == (environment / "state" / "hmac.key").read_bytes()
    restored = environment / "restored.sqlite"
    restored.write_bytes(data["davbridge.sqlite"])
    assert sqlite3.connect(restored).execute("SELECT count(*) FROM sync_state").fetchone()[0] == 1
    assert b"public_url" in data["davbridge.toml"]


def test_tar_backup_without_key(environment, capsys):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())
    target = environment / "nokey.tar"
    code, out, _ = run(["backup", "--output", str(target), "--without-key"], capsys)
    assert code == 0 and "does not contain hmac.key" in out
    with tarfile.open(target) as archive:
        assert "hmac.key" not in archive.getnames()


def test_tar_backup_needs_the_key_unless_excluded(environment, capsys):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())
    (environment / "state" / "hmac.key").unlink()
    code, _, err = run(["backup", "--output", str(environment / "k.tar")], capsys)
    assert code == 1 and "--without-key" in err and not (environment / "k.tar").exists()
    assert run(["backup", "--output", str(environment / "k.tar"), "--without-key"], capsys)[0] == 0


def test_backup_output_is_required(environment):
    with pytest.raises(SystemExit) as e:
        cli.main(["backup"])
    assert e.value.code == 2


# ------------------------------------------------------------------ migrate --check


def database(environment, version: int | None) -> Path:
    path = environment / "state" / "davbridge.sqlite"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA + SCHEMA_EAS)
    db.execute("INSERT INTO sync_state VALUES (x'01', 'u', 'f', NULL, x'00', x'00', 1, 1)")
    if version is not None:
        db.execute(f"PRAGMA user_version={version}")
    db.commit()
    db.close()
    return path


def snapshot(path: Path) -> tuple[bytes, float]:
    return path.read_bytes(), path.stat().st_mtime


def test_migrate_check_requires_the_flag(environment):
    with pytest.raises(SystemExit) as e:
        cli.main(["migrate"])
    assert e.value.code == 2


def test_migrate_check_without_database(environment, capsys):
    code, out, _ = run(["migrate", "--check"], capsys)
    assert code == 0 and "No state database" in out
    assert not (environment / "state" / "davbridge.sqlite").exists()


def test_migrate_check_current_schema_changes_nothing(environment, capsys):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())
    path = environment / "state" / "davbridge.sqlite"
    before = snapshot(path)
    time.sleep(0.01)
    code, out, _ = run(["migrate", "--check"], capsys)
    assert code == 0 and f"schema {SCHEMA_VERSION}" in out and "No migration needed" in out
    assert snapshot(path) == before


@pytest.mark.parametrize("version", [1, 2, 0])
def test_migrate_check_reports_that_an_older_database_would_be_discarded(environment, capsys, version):
    path = database(environment, version)
    before = snapshot(path)
    code, out, _ = run(["migrate", "--check"], capsys)
    assert code == 1 and "older" in out and "DISCARDS" in out
    assert "davbridge backup" in out
    assert snapshot(path) == before
    assert sqlite3.connect(path).execute("SELECT count(*) FROM sync_state").fetchone()[0] == 1


def test_migrate_check_unversioned_empty_database_needs_nothing(environment, capsys):
    path = environment / "state" / "davbridge.sqlite"
    sqlite3.connect(path).close()
    code, out, _ = run(["migrate", "--check"], capsys)
    assert code == 0 and "No migration needed" in out


def test_migrate_check_reports_that_a_newer_database_is_refused_not_discarded(environment, capsys):
    path = database(environment, SCHEMA_VERSION + 1)
    before = snapshot(path)
    code, out, err = run(["migrate", "--check"], capsys)
    assert code == 1 and "newer DAVbridge" in err and "Refusing to start so that nothing is deleted" in err and "DISCARDS" not in out
    assert snapshot(path) == before


def test_migrate_check_reports_that_a_foreign_file_is_refused(environment, capsys):
    path = environment / "state" / "davbridge.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE notes(a)")
    db.execute("PRAGMA application_id=77")
    db.commit()
    db.close()
    before = snapshot(path)
    code, out, err = run(["migrate", "--check"], capsys)
    assert code == 1 and "not a DAVbridge state database" in err and "DISCARDS" not in out
    assert snapshot(path) == before


def test_migrate_check_accepts_the_current_schema_without_application_id(environment, capsys):
    path = database(environment, SCHEMA_VERSION)
    code, out, _ = run(["migrate", "--check"], capsys)
    assert code == 0 and "application id" in out and "nothing is lost" in out
    assert sqlite3.connect(path).execute("PRAGMA application_id").fetchone()[0] == 0


def test_every_state_command_refuses_a_newer_database_with_exit_2(environment, capsys):
    path = database(environment, SCHEMA_VERSION + 1)
    before = snapshot(path)
    for argv in (["devices"], ["unblock", "--all"], ["reset", "all", "--yes-i-understand"], ["rotate-key", "--yes-i-understand"]):
        code, _, err = run(argv, capsys)
        assert code == 2 and "newer DAVbridge" in err, argv
    assert snapshot(path) == before


def test_migrate_check_reports_corruption_and_unreadable_files(environment, capsys):
    (environment / "state" / "davbridge.sqlite").write_bytes(b"not sqlite" * 500)
    code, out, err = run(["migrate", "--check"], capsys)
    assert code == 1 and ("cannot read" in err or "integrity" in err)


# ------------------------------------------------------------------ rotate-key


def test_rotate_key_needs_confirmation_and_explains_the_consequences(environment, capsys):
    before = (environment / "state" / "hmac.key").read_bytes()
    code, _, err = run(["rotate-key"], capsys)
    assert code == 2 and "--yes-i-understand" in err and "full resynchronisation" in err and "re-provisioned" in err
    assert (environment / "state" / "hmac.key").read_bytes() == before


def test_rotate_key_replaces_the_key_keeps_the_old_one_and_discards_bound_state(environment, capsys):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())
    state = environment / "state"
    old = (state / "hmac.key").read_bytes()
    code, out, _ = run(["rotate-key", "--yes-i-understand"], capsys)
    new = (state / "hmac.key").read_bytes()
    assert code == 0 and new != old and mode(state / "hmac.key") == 0o600 and len(read_key(state / "hmac.key")) == 32
    assert new.decode().strip() not in out and old.decode().strip() not in out
    kept = [p for p in state.iterdir() if p.name.startswith("hmac.key.") and p.name.endswith(".old")]
    assert len(kept) == 1 and kept[0].read_bytes() == old and mode(kept[0]) == 0o600
    assert str(kept[0]) in out and "full resynchronisation" in out and not [p for p in state.iterdir() if ".tmp-" in p.name]
    db = sqlite3.connect(state / "davbridge.sqlite")
    for table in ("sync_state", "eas_device", "eas_sync", "create_dedup", "event_log"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    assert db.execute("SELECT count(*) FROM user_configuration").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM lockout").fetchone()[0] == 1


def test_rotate_key_without_keeping_the_old_key(environment, capsys):
    state = environment / "state"
    code, out, _ = run(["rotate-key", "--yes-i-understand", "--no-keep-old"], capsys)
    assert code == 0 and not [p for p in state.iterdir() if p.name.endswith(".old")] and "previous key was kept" not in out


def test_rotate_key_refuses_while_the_service_runs(environment, capsys):
    state = environment / "state"
    (state / "status.json").write_text(json.dumps({"time": int(time.time())}))
    before = (state / "hmac.key").read_bytes()
    code, _, err = run(["rotate-key", "--yes-i-understand"], capsys)
    assert code == 1 and "Stop it first" in err and (state / "hmac.key").read_bytes() == before
    stale = time.time() - 3600
    os.utime(state / "status.json", (stale, stale))
    assert run(["rotate-key", "--yes-i-understand"], capsys)[0] == 0


def test_rotate_key_force_overrides_a_recent_status_file_and_the_refusal_says_how_long_to_wait(environment, capsys):
    state = environment / "state"
    (state / "status.json").write_text(json.dumps({"time": int(time.time())}))
    code, _, err = run(["rotate-key", "--yes-i-understand"], capsys)
    assert code == 1 and "seconds" in err and "--force" in err and "wait about" in err
    before = (state / "hmac.key").read_bytes()
    assert run(["rotate-key", "--yes-i-understand", "--force"], capsys)[0] == 0
    assert (state / "hmac.key").read_bytes() != before


def test_rotate_key_failure_before_the_discard_keeps_key_and_state(environment, capsys, monkeypatch):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())
    state = environment / "state"
    old = (state / "hmac.key").read_bytes()
    real_link = os.link

    def failing_link(src, dst, *a, **k):
        if str(dst).endswith(".old"):
            raise OSError(28, "No space left on device")
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(os, "link", failing_link)
    code, _, err = run(["rotate-key", "--yes-i-understand"], capsys)
    assert code == 1 and "nothing was changed" in err
    assert (state / "hmac.key").read_bytes() == old
    assert [p.name for p in state.iterdir() if ".tmp-" in p.name or p.name.endswith(".old")] == []
    db = sqlite3.connect(state / "davbridge.sqlite")
    assert db.execute("SELECT count(*) FROM sync_state").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM eas_device").fetchone()[0] == 1


def test_rotate_key_cannot_create_the_temporary_key_keeps_state(environment, capsys, monkeypatch):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())
    state = environment / "state"
    real_open = os.open

    def failing_open(path, flags, *a, **k):
        if ".tmp-" in str(path) and flags & os.O_CREAT:
            raise OSError(30, "Read-only file system")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", failing_open)
    code, _, err = run(["rotate-key", "--yes-i-understand"], capsys)
    monkeypatch.undo()
    assert code == 1 and "nothing was changed" in err
    assert sqlite3.connect(state / "davbridge.sqlite").execute("SELECT count(*) FROM sync_state").fetchone()[0] == 1


def test_rotate_key_discards_the_state_only_after_the_new_key_is_prepared(environment, capsys, monkeypatch):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())
    state = environment / "state"
    order = []
    real_link, real_discard, real_replace = os.link, Store.discard_key_bound, os.replace
    monkeypatch.setattr(os, "link", lambda *a, **k: (order.append("link"), real_link(*a, **k))[1])

    async def discard(self, *args, **kwargs):
        order.append("discard")
        assert any(".tmp-" in p.name for p in state.iterdir()) and any(p.name.endswith(".old") for p in state.iterdir())
        return await real_discard(self, *args, **kwargs)

    monkeypatch.setattr(Store, "discard_key_bound", discard)
    monkeypatch.setattr(os, "replace", lambda *a, **k: (order.append("replace"), real_replace(*a, **k))[1])
    assert run(["rotate-key", "--yes-i-understand"], capsys)[0] == 0
    assert order.index("link") < order.index("discard") < order.index("replace")


def test_rotate_key_failing_replace_rolls_the_state_back_and_keeps_the_backup(environment, capsys, monkeypatch):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())
    state = environment / "state"
    old = (state / "hmac.key").read_bytes()
    real_replace = os.replace

    def failing_replace(src, dst, *a, **k):
        if str(dst).endswith("hmac.key"):
            raise OSError(30, "Read-only file system")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", failing_replace)
    code, _, err = run(["rotate-key", "--yes-i-understand"], capsys)
    monkeypatch.setattr(os, "replace", real_replace)
    assert code == 1 and "cannot write the new key file" in err and "state was not touched" in err
    assert (state / "hmac.key").read_bytes() == old
    kept = [p for p in state.iterdir() if p.name.endswith(".old")]
    assert len(kept) == 1 and kept[0].read_bytes() == old and str(kept[0]) in err
    assert not [p for p in state.iterdir() if ".tmp-" in p.name]
    db = sqlite3.connect(state / "davbridge.sqlite")
    assert db.execute("SELECT count(*) FROM sync_state").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM eas_device").fetchone()[0] == 1
    db.close()
    kept[0].unlink()
    assert run(["rotate-key", "--yes-i-understand"], capsys)[0] == 0
    assert (state / "hmac.key").read_bytes() != old


def test_rotate_key_commits_the_deletion_only_after_the_replace(environment, capsys, monkeypatch):
    store = asyncio.run(populate(environment))
    asyncio.run(store.close())
    state = environment / "state"
    real_replace = os.replace
    seen = {}

    def replace(src, dst, *a, **k):
        other = sqlite3.connect(state / "davbridge.sqlite")
        seen["rows_visible_to_other_connections"] = other.execute("SELECT count(*) FROM sync_state").fetchone()[0]
        other.close()
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", replace)
    assert run(["rotate-key", "--yes-i-understand"], capsys)[0] == 0
    assert seen["rows_visible_to_other_connections"] == 1
    assert sqlite3.connect(state / "davbridge.sqlite").execute("SELECT count(*) FROM sync_state").fetchone()[0] == 0


def test_rotate_key_needs_an_existing_key(environment, capsys):
    (environment / "state" / "hmac.key").unlink()
    code, _, err = run(["rotate-key", "--yes-i-understand"], capsys)
    assert code == 1 and "davbridge init" in err
    assert not (environment / "state" / "hmac.key").exists()


def test_rotated_key_changes_every_derived_identifier(environment, capsys):
    from davbridge.identifiers import IdService

    state = environment / "state"
    before = IdService(read_key(state / "hmac.key"))
    assert run(["rotate-key", "--yes-i-understand"], capsys)[0] == 0
    after = IdService(read_key(state / "hmac.key"))
    assert before.folder_id("test-user", "test-calendar") != after.folder_id("test-user", "test-calendar")
    assert before.device_hash("test-user", "dev") != after.device_hash("test-user", "dev")
