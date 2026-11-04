import os
import sqlite3

import pytest

from davbridge.store import APPLICATION_ID, SCHEMA, SCHEMA_EAS, SCHEMA_VERSION, DatabaseRefused, Store


@pytest.fixture
async def store(tmp_path, clock):
    s = await Store.open(tmp_path / "b.sqlite", clock, failed_attempts=3, lock_s=100)
    yield s
    await s.close()


async def test_state_roundtrip_and_account_binding(store):
    zid = await store.new_state("u", "test-calendar", "tok", {"a.ics": '"1"'}, {"b.ics": None})
    z = await store.state("u", "test-calendar", zid)
    assert z.caldav_token == "tok" and z.known == {"a.ics": '"1"'} and z.open == {"b.ics": None}
    assert await store.state("x", "test-calendar", zid) is None
    assert await store.state("u", "read-only-calendar", zid) is None
    assert await store.state("u", "test-calendar", b"\0" * 16) is None


async def test_clean_up_keeps_used_and_caps_per_folder(store, clock):
    ids = []
    for i in range(205):
        clock.before(1)
        ids.append(await store.new_state("u", "test-calendar", str(i), {}, {}))
    await store.clean_up()
    assert await store.count_states() == 200
    assert await store.state("u", "test-calendar", ids[0]) is None
    assert await store.state("u", "test-calendar", ids[204]) is not None
    clock.before(8 * 86400)
    await store.clean_up()
    assert await store.count_states() == 0


async def test_used_state_survives_the_seven_days(store, clock):
    zid = await store.new_state("u", "test-calendar", "t", {}, {})
    clock.before(6 * 86400)
    assert await store.state("u", "test-calendar", zid) is not None
    clock.before(6 * 86400)
    await store.clean_up()
    assert await store.state("u", "test-calendar", zid) is not None


async def test_dedup_ttl(store, clock):
    await store.dedup_remember("u", b"k", "test-calendar", "a.ics", ttl_s=10)
    assert await store.dedup_get("u", b"k") == ("test-calendar", "a.ics")
    clock.before(11)
    assert await store.dedup_get("u", b"k") is None


async def test_lock_doubles_and_is_persistent(store, clock, tmp_path):
    for _ in range(2):
        assert await store.lock_failed_attempt("1.2.3.4") == 0
    assert await store.lock_failed_attempt("1.2.3.4") == 100
    assert await store.lock_check("1.2.3.4") == 100
    clock.before(101)
    assert await store.lock_check("1.2.3.4") == 0
    for _ in range(2):
        await store.lock_failed_attempt("1.2.3.4")
    assert await store.lock_failed_attempt("1.2.3.4") == 200
    assert await store.lock_lift("1.2.3.4") == 1
    assert await store.lock_check("1.2.3.4") == 0


async def test_config_new_and_size(store):
    assert await store.config_set("u", "calendar", "x", b"<a/>", new=True)
    assert not await store.config_set("u", "calendar", "x", b"<b/>", new=True)
    assert await store.config_set("u", "calendar", "x", b"<b/>", new=False)
    assert await store.config_get("u", "calendar", "x") == b"<b/>"
    with pytest.raises(ValueError):
        await store.config_set("u", "calendar", "y", b"x" * (256 * 1024 + 1), new=False)
    assert await store.config_delete("u", "calendar", "x")


def _set_pragma(path, **pragmas):
    db = sqlite3.connect(path)
    for name, value in pragmas.items():
        db.execute(f"PRAGMA {name}={value}")
    db.commit()
    db.close()


def _pragma(path, name):
    db = sqlite3.connect(path)
    try:
        return db.execute(f"PRAGMA {name}").fetchone()[0]
    finally:
        db.close()


async def _populated(path, clock):
    s = await Store.open(path, clock)
    await s.new_state("u", "test-calendar", None, {}, {})
    await s.lock_failed_attempt("203.0.113.9")
    await s.close()


async def test_a_new_database_carries_the_application_id_and_the_schema_version(tmp_path, clock):
    path = tmp_path / "b.sqlite"
    s = await Store.open(path, clock)
    await s.close()
    assert _pragma(path, "application_id") == APPLICATION_ID == 0x44415642 and _pragma(path, "user_version") == SCHEMA_VERSION


async def test_an_older_schema_is_discarded_and_recreated(tmp_path, clock):
    path = tmp_path / "b.sqlite"
    await _populated(path, clock)
    _set_pragma(path, user_version=SCHEMA_VERSION - 1)
    s = await Store.open(path, clock)
    assert await s.count_states() == 0
    await s.close()
    assert _pragma(path, "application_id") == APPLICATION_ID and _pragma(path, "user_version") == SCHEMA_VERSION


async def test_a_database_without_application_id_but_with_the_current_schema_is_adopted_not_discarded(tmp_path, clock):
    path = tmp_path / "b.sqlite"
    await _populated(path, clock)
    _set_pragma(path, application_id=0)
    s = await Store.open(path, clock)
    assert await s.count_states() == 1
    await s.close()
    assert _pragma(path, "application_id") == APPLICATION_ID


async def test_a_newer_schema_is_refused_and_nothing_is_deleted(tmp_path, clock):
    path = tmp_path / "b.sqlite"
    await _populated(path, clock)
    _set_pragma(path, user_version=SCHEMA_VERSION + 1)
    before = path.read_bytes()
    with pytest.raises(DatabaseRefused, match="newer DAVbridge"):
        await Store.open(path, clock)
    assert path.read_bytes() == before and _pragma(path, "user_version") == SCHEMA_VERSION + 1
    assert sqlite3.connect(path).execute("SELECT count(*) FROM sync_state").fetchone()[0] == 1


async def test_a_downgrade_after_a_newer_start_never_wipes_the_database(tmp_path, clock, monkeypatch):
    path = tmp_path / "b.sqlite"
    with monkeypatch.context() as newer:
        newer.setattr("davbridge.store.SCHEMA_VERSION", SCHEMA_VERSION + 1)
        await _populated(path, clock)
    assert _pragma(path, "user_version") == SCHEMA_VERSION + 1
    for _ in range(2):
        with pytest.raises(DatabaseRefused):
            await Store.open(path, clock)
    assert sqlite3.connect(path).execute("SELECT count(*) FROM lockout").fetchone()[0] == 1


@pytest.mark.parametrize("prepare", ["other-application-id", "other-tables", "other-tables-with-version", "our-tables-other-id"])
async def test_a_foreign_sqlite_file_is_refused_untouched(tmp_path, clock, prepare):
    path = tmp_path / "foreign.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE notes(a)")
    db.execute("INSERT INTO notes VALUES (1)")
    if prepare == "other-application-id":
        db.execute("PRAGMA application_id=1234")
    if prepare == "other-tables-with-version":
        db.execute("PRAGMA user_version=1")
    if prepare == "our-tables-other-id":
        db.execute("CREATE TABLE sync_state(id)")
        db.execute("CREATE TABLE lockout(ip)")
        db.execute("PRAGMA application_id=1234")
    db.commit()
    db.close()
    before = path.read_bytes()
    with pytest.raises(DatabaseRefused, match="not a DAVbridge state database"):
        await Store.open(path, clock)
    assert path.read_bytes() == before and not (tmp_path / "foreign.sqlite-wal").exists()


def _own_layout_with(path, *, drop=(), extra=()):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA + SCHEMA_EAS)
    for statement in extra:
        db.execute(statement)
    for table in drop:
        db.execute(f"DROP TABLE {table}")
    db.execute("PRAGMA user_version=2")
    db.commit()
    db.close()


@pytest.mark.parametrize(
    "prepare",
    [
        "two-names-only",
        "two-names-and-notes",
        "own-tables-plus-a-foreign-table",
        "own-tables-with-an-extra-column",
        "own-tables-with-a-missing-eas-table",
    ],
)
async def test_a_file_without_application_id_is_ours_only_with_an_exact_schema_fingerprint(tmp_path, clock, prepare):
    path = tmp_path / "foreign.sqlite"
    if prepare == "two-names-only":
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE sync_state(id)")
        db.execute("CREATE TABLE lockout(ip)")
        db.execute("PRAGMA user_version=2")
        db.commit()
        db.close()
    elif prepare == "two-names-and-notes":
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE sync_state(id, a, b)")
        db.execute("CREATE TABLE lockout(ip, c)")
        db.execute("CREATE TABLE notes(a)")
        db.execute("INSERT INTO notes VALUES (1)")
        db.execute("PRAGMA user_version=1")
        db.commit()
        db.close()
    elif prepare == "own-tables-plus-a-foreign-table":
        _own_layout_with(path, extra=["CREATE TABLE notes(a)"])
    elif prepare == "own-tables-with-an-extra-column":
        _own_layout_with(path, extra=["ALTER TABLE lockout ADD COLUMN note TEXT"])
    else:
        _own_layout_with(path, drop=["eas_folder"])
    before = path.read_bytes()
    with pytest.raises(DatabaseRefused, match="not a DAVbridge state database"):
        await Store.open(path, clock)
    assert path.read_bytes() == before and not (tmp_path / "foreign.sqlite-wal").exists()


async def test_an_older_database_without_application_id_and_with_our_exact_schema_is_still_recreated(tmp_path, clock):
    path = tmp_path / "old.sqlite"
    _own_layout_with(path)
    db = sqlite3.connect(path)
    db.execute("INSERT INTO lockout(ip, failures) VALUES ('203.0.113.9', 1)")
    db.commit()
    db.close()
    s = await Store.open(path, clock)
    await s.close()
    assert _pragma(path, "application_id") == APPLICATION_ID and _pragma(path, "user_version") == SCHEMA_VERSION
    assert sqlite3.connect(path).execute("SELECT count(*) FROM lockout").fetchone()[0] == 0


async def test_a_database_of_the_current_schema_without_the_eas_tables_is_adopted(tmp_path, clock):
    path = tmp_path / "b.sqlite"
    await _populated(path, clock)
    db = sqlite3.connect(path)
    for table in ("eas_sync", "eas_folder", "eas_device"):
        db.execute(f"DROP TABLE {table}")
    db.execute("PRAGMA application_id=0")
    db.commit()
    db.close()
    s = await Store.open(path, clock)
    assert await s.count_states() == 1
    await s.close()


async def test_a_file_that_is_not_sqlite_is_refused_untouched(tmp_path, clock):
    path = tmp_path / "junk.sqlite"
    path.write_bytes(b"this is not a database" * 100)
    with pytest.raises(DatabaseRefused, match="not a readable SQLite database"):
        await Store.open(path, clock)
    assert path.read_bytes() == b"this is not a database" * 100


async def test_an_empty_file_is_initialised(tmp_path, clock):
    path = tmp_path / "empty.sqlite"
    path.write_bytes(b"")
    s = await Store.open(path, clock)
    await s.close()
    assert _pragma(path, "application_id") == APPLICATION_ID


async def test_event_field_allowlist(store):
    await store.event("u", "detected", folder="test-calendar", element="abc", value=1)
    with pytest.raises(ValueError):
        await store.event("u", "detected", subject="secret")


async def test_clean_up_removes_old_failed_attempt_rows_and_caps_the_table(store, clock, monkeypatch):
    for i in range(3):
        await store.lock_failed_attempt(f"10.0.0.{i}")
    clock.before(2000)
    await store.clean_up()
    assert store.db.execute("SELECT count(*) FROM lockout").fetchone()[0] == 0
    monkeypatch.setattr("davbridge.store.MAX_LOCK_LINES", 5)
    for i in range(20):
        await store.lock_failed_attempt(f"10.1.0.{i}")
    await store.clean_up()
    assert store.db.execute("SELECT count(*) FROM lockout").fetchone()[0] == 5


async def test_success_resets_level_blocked_ip_stays_blocked(store, clock):
    for _ in range(3):
        await store.lock_failed_attempt("1.2.3.4")
    await store.lock_success("1.2.3.4")
    assert await store.lock_check("1.2.3.4") > 0
    clock.before(101)
    await store.lock_success("1.2.3.4")
    for _ in range(3):
        await store.lock_failed_attempt("1.2.3.4")
    assert await store.lock_check("1.2.3.4") == 100


def _mode(path) -> int:
    return path.stat().st_mode & 0o777


async def test_database_and_wal_files_are_owner_only(tmp_path, clock):
    path = tmp_path / "state" / "d.sqlite"
    old_umask = os.umask(0o000)
    try:
        s = await Store.open(path, clock)
        await s.lock_failed_attempt("203.0.113.9")
        assert _mode(path) == 0o600
        for suffix in ("-wal", "-shm"):
            assert (tmp_path / "state" / f"d.sqlite{suffix}").exists()
            assert _mode(tmp_path / "state" / f"d.sqlite{suffix}") == 0o600
        assert _mode(path.parent) == 0o700
        await s.close()
    finally:
        os.umask(old_umask)


async def test_existing_world_readable_database_is_tightened(tmp_path, clock):
    path = tmp_path / "d.sqlite"
    s = await Store.open(path, clock)
    await s.close()
    os.chmod(path, 0o644)
    s = await Store.open(path, clock)
    assert _mode(path) == 0o600
    await s.close()


async def test_recreated_database_after_a_schema_change_is_owner_only(tmp_path, clock):
    path = tmp_path / "d.sqlite"
    _own_layout_with(path)
    os.chmod(path, 0o644)
    s = await Store.open(path, clock)
    assert _mode(path) == 0o600
    await s.close()
