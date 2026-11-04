"""SQLite metadata. Disposable metadata only, never appointment content. All methods are async and run in one thread."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import zlib
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from .clock import Clock

SCHEMA_VERSION = 3
APPLICATION_ID = 0x44415642
"""`PRAGMA application_id` of every DAVbridge state database ("DAVB"): a foreign SQLite file at the same path is recognised and never touched."""
DATABASE_MODE = 0o600
KEEP_STATES = 200
DAYS_7 = 7 * 86400
DAYS_30 = 30 * 86400
MAX_LOCK_S = 86400
ERROR_WINDOW_S = 900
MAX_LOCK_LINES = 5000
MAX_CONFIG_BYTES = 256 * 1024
MAX_STATE_BYTES = 4 * 1024 * 1024

T = TypeVar("T")

SCHEMA = """
CREATE TABLE sync_state (
  id BLOB PRIMARY KEY, account TEXT NOT NULL, folder TEXT NOT NULL, caldav_token TEXT,
  known BLOB NOT NULL, open BLOB NOT NULL, created INTEGER NOT NULL, used INTEGER NOT NULL);
CREATE INDEX sync_state_folder ON sync_state(account, folder, created DESC);
CREATE TABLE user_configuration (
  account TEXT NOT NULL, folder TEXT NOT NULL, name TEXT NOT NULL, data BLOB NOT NULL, changed INTEGER NOT NULL,
  PRIMARY KEY (account, folder, name));
CREATE TABLE create_dedup (
  account TEXT NOT NULL, dedup_key BLOB NOT NULL, cal TEXT NOT NULL, href TEXT NOT NULL, expires INTEGER NOT NULL,
  PRIMARY KEY (account, dedup_key));
CREATE TABLE lockout (ip TEXT PRIMARY KEY, failures INTEGER NOT NULL, blocked_until INTEGER NOT NULL DEFAULT 0, level INTEGER NOT NULL DEFAULT 0, last_seen INTEGER NOT NULL DEFAULT 0);
CREATE TABLE event_log (
  time REAL NOT NULL, account TEXT NOT NULL, kind TEXT NOT NULL, folder TEXT, element TEXT, etag_hash TEXT, value INTEGER);
CREATE INDEX event_log_time ON event_log(time);
"""


SCHEMA_EAS = """
CREATE TABLE IF NOT EXISTS eas_device (
  account TEXT NOT NULL, device TEXT NOT NULL, type TEXT NOT NULL, agent TEXT, model TEXT, os TEXT,
  created INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  PRIMARY KEY (account, device));
CREATE TABLE IF NOT EXISTS eas_sync (
  account TEXT NOT NULL, device TEXT NOT NULL, folder TEXT NOT NULL,
  counter INTEGER NOT NULL, caldav_token TEXT,
  known BLOB NOT NULL, open BLOB NOT NULL, plan BLOB, request_hash BLOB, options TEXT,
  last_seen INTEGER NOT NULL,
  PRIMARY KEY (account, device, folder),
  FOREIGN KEY (account, device) REFERENCES eas_device(account, device) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS eas_sync_last_seen ON eas_sync(last_seen);
CREATE TABLE IF NOT EXISTS eas_folder (
  account TEXT NOT NULL, device TEXT NOT NULL, folder TEXT NOT NULL, name TEXT NOT NULL, type INTEGER NOT NULL, created INTEGER NOT NULL,
  PRIMARY KEY (account, device, folder),
  FOREIGN KEY (account, device) REFERENCES eas_device(account, device) ON DELETE CASCADE);
"""
MAX_OWN_FOLDER = 20
MAX_PLAN_BYTES = 512 * 1024
MAX_OPTIONS_BYTES = 2048
EAS_QUIET_DISPLACE_S = 14 * 86400


@dataclass(frozen=True, slots=True)
class EasSync:
    account: str
    device: str
    folder: str
    counter: int
    caldav_token: str | None
    known: Mapping[str, str]
    open: Mapping[str, str | None]
    plan: Mapping | None
    request_hash: bytes | None
    options: Mapping
    last: int


@dataclass(frozen=True, slots=True)
class SyncState:
    id: bytes
    account: str
    folder: str
    caldav_token: str | None
    known: Mapping[str, str]
    open: Mapping[str, str | None]
    created: int
    used: int


def _pack(data: Mapping) -> bytes:
    raw = zlib.compress(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError("state too large")
    return raw


def _unpack(blob: bytes) -> dict:
    return json.loads(zlib.decompress(blob).decode("utf-8"))


class Verdict(StrEnum):
    """What the service does with the file at its database path."""

    NEW = "new"  # no file, an empty file or no tables at all: created with the current schema
    CURRENT = "current"
    ADOPT = "adopt"  # current schema written before the application id existed: only the id is added
    OLDER = "older"  # an older schema of ours: discarded and recreated (there is no in-place migration)
    NEWER = "newer"  # written by a newer DAVbridge: the service refuses to start and deletes nothing
    FOREIGN = "foreign"  # some other SQLite file: the service refuses to start and touches nothing


Layout = dict[str, frozenset[tuple[str, str]]]  # table name -> (column name, declared type) of every column


def _layout_of(db: sqlite3.Connection) -> Layout:
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'").fetchall()]
    return {n: frozenset((r[1], r[2].upper()) for r in db.execute(f'PRAGMA table_info("{n}")').fetchall()) for n in names}


def _known_layouts() -> tuple[Layout, ...]:
    """Fingerprints of the state databases DAVbridge itself ever wrote before it stamped its application id: the English schema with and without the
    EAS tables (they are added after the base schema). The German pre-release schema used other table names and was never published."""
    result: list[Layout] = []
    for script in (SCHEMA, SCHEMA + SCHEMA_EAS):
        db = sqlite3.connect(":memory:")
        try:
            db.executescript(script)
            result.append(_layout_of(db))
        finally:
            db.close()
    return tuple(result)


KNOWN_LAYOUTS = _known_layouts()


def classify(application_id: int, schema_version: int, layout: Layout) -> Verdict:
    """A file without our application id counts as an older DAVbridge database only if its tables and columns match a known layout exactly:
    two matching table names are not enough to delete somebody else's file."""
    tables = layout
    ours = layout in KNOWN_LAYOUTS
    if application_id == APPLICATION_ID:
        if schema_version == SCHEMA_VERSION:
            return Verdict.CURRENT
        return Verdict.NEWER if schema_version > SCHEMA_VERSION else Verdict.OLDER
    if application_id != 0:
        return Verdict.FOREIGN
    if not tables and schema_version == 0:
        return Verdict.NEW
    if not ours:
        return Verdict.FOREIGN
    if schema_version == SCHEMA_VERSION:
        return Verdict.ADOPT
    return Verdict.NEWER if schema_version > SCHEMA_VERSION else Verdict.OLDER


class DatabaseRefused(Exception):
    """The file at the database path must not be opened or replaced (newer schema or not a DAVbridge database); the message says what to do."""


def refusal_message(path: Path, verdict: Verdict, schema_version: int | None) -> str:
    if verdict == Verdict.NEWER:
        return (
            f"the state database {path} was written by a newer DAVbridge (schema {schema_version}; this version uses schema {SCHEMA_VERSION}). Refusing to start so that nothing is deleted: "
            "run the newer version again, or restore a backup made with this version, or move the file away yourself"
        )
    return f"{path} is not a DAVbridge state database (it has another application id or other tables). Refusing to touch it: check DAVBRIDGE_STATE_DIR or move the file away yourself"


@dataclass(frozen=True, slots=True)
class DatabaseInfo:
    """Read-only facts about a database file (no schema change, nothing is created or discarded)."""

    exists: bool
    schema_version: int | None = None
    integrity: str = ""
    size_bytes: int = 0
    sync_states: int = 0
    devices: int = 0
    active_locks: int = 0
    application_id: int = 0
    verdict: Verdict = Verdict.NEW


class DatabaseError(Exception):
    """The file is not a readable SQLite database, or a statement on it failed."""


def inspect_database(path: Path, now: float, *, integrity: bool = True) -> DatabaseInfo:
    """Opens the file read-only. Raises DatabaseError if it is not a readable SQLite database. `integrity=False` skips the full integrity check."""
    if not path.is_file():
        return DatabaseInfo(False)
    try:
        return _inspect(path, now, integrity)
    except sqlite3.Error as e:
        raise DatabaseError(type(e).__name__) from None


def _inspect(path: Path, now: float, check_integrity: bool = True) -> DatabaseInfo:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        application_id = db.execute("PRAGMA application_id").fetchone()[0]
        integrity = "; ".join(r[0] for r in db.execute("PRAGMA integrity_check").fetchall()) if check_integrity else ""
        layout = _layout_of(db)
        tables = set(layout)

        def count(sql: str, table: str, *args: Any) -> int:
            if table not in tables:
                return 0
            try:
                return db.execute(sql, args).fetchone()[0]
            except sqlite3.OperationalError:
                return 0  # a file with the same table name but another layout is classified by its ids, not counted

        return DatabaseInfo(
            True,
            version,
            integrity,
            path.stat().st_size,
            count("SELECT count(*) FROM sync_state", "sync_state"),
            count("SELECT count(*) FROM eas_device", "eas_device"),
            count("SELECT count(*) FROM lockout WHERE blocked_until>?", "lockout", now),
            application_id,
            classify(application_id, version, layout),
        )
    finally:
        db.close()


def backup_database(source: Path, target: Path) -> str:
    """Consistent online copy through the SQLite backup API (safe while the service writes in WAL mode). `target` must already exist
    as an empty file the caller created with the right mode. Returns the result of the integrity check of the copy. Raises DatabaseError."""
    try:
        return _backup(source, target)
    except sqlite3.Error as e:
        raise DatabaseError(type(e).__name__) from None


def _backup(source: Path, target: Path) -> str:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            return "; ".join(r[0] for r in dst.execute("PRAGMA integrity_check").fetchall())
        finally:
            dst.close()
    finally:
        src.close()


class Store:
    def __init__(self, path: Path, clock: Clock, failed_attempts: int, lock_s: int) -> None:
        self._path = path
        self._clock = clock
        self._failed_attempts = failed_attempts
        self._lock_s = lock_s
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="store")
        self._db: sqlite3.Connection | None = None

    @classmethod
    async def open(cls, path: Path, clock: Clock, failed_attempts: int = 10, lock_s: int = 900) -> Store:
        s = cls(path, clock, failed_attempts, lock_s)
        await s._run(s._init)
        return s

    async def close(self) -> None:
        def to() -> None:
            if self._db is not None:
                self._db.close()
                self._db = None

        await self._run(to)
        self._pool.shutdown(wait=True)

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    def _secure_files(self) -> None:
        """The database holds client addresses and sync metadata: create it owner-only and tighten files left behind by older versions.
        SQLite gives the -wal and -shm files the mode of the database file."""
        try:
            os.close(os.open(self._path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), DATABASE_MODE))
        except OSError:
            return
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(str(self._path) + suffix, DATABASE_MODE)
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        self._secure_files()
        db = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            info = inspect_database(self._path, self._clock.now(), integrity=False)
        except DatabaseError:
            raise DatabaseRefused(f"{self._path} is not a readable SQLite database. Refusing to touch it: restore a backup or move the file away yourself") from None
        verdict = info.verdict
        if verdict in (Verdict.NEWER, Verdict.FOREIGN):
            raise DatabaseRefused(refusal_message(self._path, verdict, info.schema_version))
        if verdict == Verdict.OLDER:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(str(self._path) + suffix)
                except FileNotFoundError:
                    pass
        db = self._connect()
        if verdict in (Verdict.NEW, Verdict.OLDER):
            db.executescript("BEGIN;" + SCHEMA + f"PRAGMA application_id={APPLICATION_ID};PRAGMA user_version={SCHEMA_VERSION};COMMIT;")
        elif verdict == Verdict.ADOPT:
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.executescript("BEGIN;" + SCHEMA_EAS + "COMMIT;")
        self._secure_files()
        self._db = db

    @property
    def db(self) -> sqlite3.Connection:
        assert self._db is not None
        return self._db

    def _now(self) -> int:
        return int(self._clock.now())

    async def healthy(self) -> bool:
        def probe() -> bool:
            self.db.execute("SELECT 1").fetchone()
            self.db.execute("DELETE FROM lockout WHERE ip = '__probe__'")
            return True

        try:
            return await self._run(probe)
        except sqlite3.Error:
            return False

    async def state(self, account: str, folder: str, id: bytes) -> SyncState | None:
        def read() -> SyncState | None:
            z = self.db.execute("SELECT id,account,folder,caldav_token,known,open,created,used FROM sync_state WHERE id=? AND account=? AND folder=?", (id, account, folder)).fetchone()
            if z is None:
                return None
            now = self._now()
            self.db.execute("UPDATE sync_state SET used=? WHERE id=?", (now, id))
            return SyncState(z[0], z[1], z[2], z[3], _unpack(z[4]), _unpack(z[5]), z[6], now)

        return await self._run(read)

    async def new_state(self, account: str, folder: str, caldav_token: str | None, known: Mapping[str, str], open: Mapping[str, str | None]) -> bytes:
        new_id = os.urandom(16)
        b, o = _pack(known), _pack(open)

        def write() -> None:
            now = self._now()
            self.db.execute("INSERT INTO sync_state(id,account,folder,caldav_token,known,open,created,used) VALUES(?,?,?,?,?,?,?,?)", (new_id, account, folder, caldav_token, b, o, now, now))

        await self._run(write)
        return new_id

    async def discard_states(self, account: str, folder: str | None = None) -> int:
        def delete() -> int:
            if folder is None:
                return self.db.execute("DELETE FROM sync_state WHERE account=?", (account,)).rowcount
            return self.db.execute("DELETE FROM sync_state WHERE account=? AND folder=?", (account, folder)).rowcount

        return await self._run(delete)

    async def discard_all(self) -> int:
        """Deletes every row of every table (state reset). Schema, `hmac.key` and the configuration are not touched."""

        def delete() -> int:
            tables = [z[0] for z in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
            return sum(self.db.execute(f'DELETE FROM "{t}"').rowcount for t in tables)

        return await self._run(delete)

    async def discard_key_bound(self, publish: Callable[[], None] | None = None) -> int:
        """Rows whose identifiers depend on the HMAC key (sync states, EAS devices with their sync state, create de-duplication, conflict anchors).
        Used after a key rotation; lock-outs and stored Outlook settings are not bound to the key and stay.
        The deletion is one transaction. With `publish` (the step that installs the new key) the transaction is committed only after `publish`
        returned; if it raises, the deletion is rolled back and the state survives together with the old key."""

        def delete() -> int:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                count = sum(self.db.execute(f"DELETE FROM {t}").rowcount for t in ("sync_state", "eas_device", "create_dedup", "event_log"))
                if publish is not None:
                    publish()
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
            return count

        try:
            return await self._run(delete)
        except sqlite3.Error as e:
            raise DatabaseError(str(e)) from e

    async def count_states(self) -> int:
        return await self._run(lambda: self.db.execute("SELECT count(*) FROM sync_state").fetchone()[0])

    async def config_get(self, account: str, folder: str, name: str) -> bytes | None:
        def read() -> bytes | None:
            z = self.db.execute("SELECT data FROM user_configuration WHERE account=? AND folder=? AND name=?", (account, folder, name)).fetchone()
            return z[0] if z else None

        return await self._run(read)

    async def config_set(self, account: str, folder: str, name: str, data: bytes, *, new: bool) -> bool:
        """new=True and present => False (the caller reports ErrorItemSave). More than 256 KiB => ValueError."""
        if len(data) > MAX_CONFIG_BYTES:
            raise ValueError("UserConfiguration too large")

        def write() -> bool:
            if new:
                present = self.db.execute("SELECT 1 FROM user_configuration WHERE account=? AND folder=? AND name=?", (account, folder, name)).fetchone()
                if present:
                    return False
            self.db.execute("INSERT OR REPLACE INTO user_configuration(account,folder,name,data,changed) VALUES(?,?,?,?,?)", (account, folder, name, data, self._now()))
            return True

        return await self._run(write)

    async def config_delete(self, account: str, folder: str, name: str) -> bool:
        return await self._run(lambda: self.db.execute("DELETE FROM user_configuration WHERE account=? AND folder=? AND name=?", (account, folder, name)).rowcount > 0)

    async def dedup_get(self, account: str, key: bytes) -> tuple[str, str] | None:
        def read() -> tuple[str, str] | None:
            z = self.db.execute("SELECT cal,href FROM create_dedup WHERE account=? AND dedup_key=? AND expires>?", (account, key, self._now())).fetchone()
            return (z[0], z[1]) if z else None

        return await self._run(read)

    async def dedup_remember(self, account: str, key: bytes, cal: str, href: str, ttl_s: int = 600) -> None:
        await self._run(lambda: self.db.execute("INSERT OR REPLACE INTO create_dedup(account,dedup_key,cal,href,expires) VALUES(?,?,?,?,?)", (account, key, cal, href, self._now() + ttl_s)))

    async def lock_check(self, ip: str) -> int:
        def read() -> int:
            z = self.db.execute("SELECT blocked_until FROM lockout WHERE ip=?", (ip,)).fetchone()
            return max(0, z[0] - self._now()) if z else 0

        return await self._run(read)

    async def lock_failed_attempt(self, ip: str, failed_attempts: int | None = None) -> int:
        limit = failed_attempts or self._failed_attempts

        def count() -> int:
            now = self._now()
            z = self.db.execute("SELECT failures,blocked_until,level,last_seen FROM lockout WHERE ip=?", (ip,)).fetchone()
            error, until, level, last = z if z else (0, 0, 0, 0)
            if until > now:
                return until - now
            if now - last > ERROR_WINDOW_S:
                error = 0
            if now - last > MAX_LOCK_S:
                level = 0
            error += 1
            if error >= limit:
                duration = min(MAX_LOCK_S, self._lock_s * (2**level))
                until, level, error = now + duration, level + 1, 0
            self.db.execute("INSERT OR REPLACE INTO lockout(ip,failures,blocked_until,level,last_seen) VALUES(?,?,?,?,?)", (ip, error, until, level, now))
            return max(0, until - now)

        return await self._run(count)

    async def lock_success(self, ip: str) -> None:
        await self._run(lambda: self.db.execute("UPDATE lockout SET failures=0, level=0 WHERE ip=? AND blocked_until<=? AND (failures>0 OR level>0)", (ip, self._now())))

    async def lock_lift(self, ip: str | None) -> int:
        if ip is None:
            return await self._run(lambda: self.db.execute("DELETE FROM lockout").rowcount)
        return await self._run(lambda: self.db.execute("DELETE FROM lockout WHERE ip=?", (ip,)).rowcount)

    async def locks_list(self) -> list[str]:
        return await self._run(lambda: [z[0] for z in self.db.execute("SELECT ip FROM lockout").fetchall()])

    async def locks_active(self) -> int:
        return await self._run(lambda: self.db.execute("SELECT count(*) FROM lockout WHERE blocked_until>?", (self._now(),)).fetchone()[0])

    async def event(self, account: str, kind: str, **fields: int | str | None) -> None:
        allowed = {"folder", "element", "etag_hash", "value"}
        if not set(fields) <= allowed:
            raise ValueError("unknown event field")
        await self._run(
            lambda: self.db.execute(
                "INSERT INTO event_log(time,account,kind,folder,element,etag_hash,value) VALUES(?,?,?,?,?,?,?)",
                (self._clock.now(), account, kind, fields.get("folder"), fields.get("element"), fields.get("etag_hash"), fields.get("value")),
            )
        )

    async def last_state(self, account: str, folder: str, element: str) -> str | None:
        """Conflict anchor: the ETag hash Outlook saw last (delivered to Outlook or written by Outlook)."""

        def read() -> str | None:
            line = self.db.execute(
                "SELECT etag_hash FROM event_log WHERE account=? AND folder=? AND element=? AND kind IN ('delivered','written') AND etag_hash IS NOT NULL ORDER BY time DESC, rowid DESC LIMIT 1",
                (account, folder, element),
            ).fetchone()
            return line[0] if line else None

        return await self._run(read)

    async def events(self, kind: str | None = None, since: float = 0.0, element: str | None = None) -> list[tuple]:
        def read() -> list[tuple]:
            sql, arg = "SELECT time,account,kind,folder,element,etag_hash,value FROM event_log WHERE time>=?", [since]
            if kind:
                sql, arg = sql + " AND kind=?", arg + [kind]
            if element:
                sql, arg = sql + " AND element=?", arg + [element]
            return self.db.execute(sql + " ORDER BY time", arg).fetchall()

        return await self._run(read)

    async def count_events(self, kind: str, since: float) -> int:
        return await self._run(lambda: self.db.execute("SELECT count(*) FROM event_log WHERE kind=? AND time>=?", (kind, since)).fetchone()[0])

    async def clean_up(self) -> None:
        def clean_up() -> None:
            now = self._now()
            self.db.execute(
                "DELETE FROM sync_state WHERE id IN (SELECT id FROM (SELECT id, used, row_number() OVER (PARTITION BY account, folder ORDER BY created DESC) AS r FROM sync_state) "
                "WHERE r > ? OR used < ?)",
                (KEEP_STATES, now - DAYS_7),
            )
            self.db.execute("DELETE FROM create_dedup WHERE expires<?", (now,))
            self.db.execute("DELETE FROM event_log WHERE time<?", (now - DAYS_30,))
            self.db.execute("DELETE FROM lockout WHERE blocked_until <= ? AND (last_seen + ? < ? OR (level = 0 AND last_seen + ? < ?))", (now, MAX_LOCK_S, now, ERROR_WINDOW_S, now))
            self.db.execute("DELETE FROM lockout WHERE ip IN (SELECT ip FROM lockout WHERE blocked_until <= ? ORDER BY last_seen DESC LIMIT -1 OFFSET ?)", (now, MAX_LOCK_LINES))
            self.db.execute("DELETE FROM eas_device WHERE last_seen < ?", (now - self._eas_expiry_s,))
            self.db.execute("DELETE FROM eas_sync WHERE last_seen < ?", (now - DAYS_30,))

        await self._run(clean_up)

    # ------------------------------------------------------------------ EAS (additive, same user_version)

    _eas_expiry_s: int = DAYS_30

    def set_eas_expiry(self, days: int) -> None:
        self._eas_expiry_s = days * 86400

    async def eas_device_see(self, account: str, device: str, type: str, agent: str | None, device_limit: int, expiry_s: int = EAS_QUIET_DISPLACE_S) -> str:
        """ "known", "new" or "displaced": once the limit is reached, a new device always displaces the one unused for the
        longest time (the state can be rebuilt); never a lockout."""

        def run() -> str:
            now = self._now()
            z = self.db.execute("SELECT 1 FROM eas_device WHERE account=? AND device=?", (account, device)).fetchone()
            if z:
                self.db.execute("UPDATE eas_device SET last_seen=?, type=?, agent=COALESCE(?, agent) WHERE account=? AND device=?", (now, type, agent, account, device))
                return "known"
            count = self.db.execute("SELECT count(*) FROM eas_device WHERE account=?", (account,)).fetchone()[0]
            result = "new"
            while count >= device_limit:
                old = self.db.execute("SELECT device FROM eas_device WHERE account=? ORDER BY last_seen ASC LIMIT 1", (account,)).fetchone()
                if old is None:
                    break
                self.db.execute("DELETE FROM eas_sync WHERE account=? AND device=?", (account, old[0]))
                self.db.execute("DELETE FROM eas_device WHERE account=? AND device=?", (account, old[0]))
                count -= 1
                result = "displaced"
            self.db.execute("INSERT INTO eas_device(account,device,type,agent,created,last_seen) VALUES(?,?,?,?,?,?)", (account, device, type, agent, now, now))
            return result

        return await self._run(run)

    async def eas_device_info(self, account: str, device: str, model: str | None, os_: str | None) -> None:
        await self._run(lambda: self.db.execute("UPDATE eas_device SET model=COALESCE(?, model), os=COALESCE(?, os) WHERE account=? AND device=?", (model, os_, account, device)))

    async def eas_sync_read(self, account: str, device: str, folder: str) -> EasSync | None:
        def read() -> EasSync | None:
            z = self.db.execute(
                "SELECT counter,caldav_token,known,open,plan,request_hash,options,last_seen FROM eas_sync WHERE account=? AND device=? AND folder=?", (account, device, folder)
            ).fetchone()
            if z is None:
                return None
            return EasSync(account, device, folder, z[0], z[1], _unpack(z[2]), _unpack(z[3]), _unpack(z[4]) if z[4] else None, z[5], json.loads(z[6]) if z[6] else {}, z[7])

        return await self._run(read)

    async def eas_sync_write(self, z: EasSync, *, expected: int | None) -> bool:
        """CAS: `expected=None` => create/replace at SyncKey 0; otherwise UPDATE only if the counter is unchanged."""
        b, o = _pack(z.known), _pack(z.open)
        p = _pack(z.plan) if z.plan is not None else None
        if p is not None and len(p) > MAX_PLAN_BYTES:
            raise ValueError("plan too large")
        opt = json.dumps(z.options, sort_keys=True, separators=(",", ":"))
        if len(opt) > MAX_OPTIONS_BYTES:
            raise ValueError("options too large")

        def write() -> bool:
            now = self._now()
            if expected is None:
                self.db.execute(
                    "INSERT OR REPLACE INTO eas_sync(account,device,folder,counter,caldav_token,known,open,plan,request_hash,options,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (z.account, z.device, z.folder, z.counter, z.caldav_token, b, o, p, z.request_hash, opt, now),
                )
                return True
            return (
                self.db.execute(
                    "UPDATE eas_sync SET counter=?, caldav_token=?, known=?, open=?, plan=?, request_hash=?, options=?, last_seen=? WHERE account=? AND device=? AND folder=? AND counter=?",
                    (z.counter, z.caldav_token, b, o, p, z.request_hash, opt, now, z.account, z.device, z.folder, expected),
                ).rowcount
                == 1
            )

        return await self._run(write)

    async def eas_touch(self, account: str, device: str, folder: str) -> None:
        await self._run(lambda: self.db.execute("UPDATE eas_sync SET last_seen=? WHERE account=? AND device=? AND folder=?", (self._now(), account, device, folder)))

    async def eas_delete(self, account: str | None, device: str | None = None, folder: str | None = None) -> int:
        def delete() -> int:
            if account is None:
                n = self.db.execute("DELETE FROM eas_sync").rowcount
                return n + self.db.execute("DELETE FROM eas_device").rowcount
            if device is None:
                n = self.db.execute("DELETE FROM eas_sync WHERE account=?", (account,)).rowcount
                return n + self.db.execute("DELETE FROM eas_device WHERE account=?", (account,)).rowcount
            if folder is None:
                n = self.db.execute("DELETE FROM eas_sync WHERE account=? AND device=?", (account, device)).rowcount
                return n + self.db.execute("DELETE FROM eas_device WHERE account=? AND device=?", (account, device)).rowcount
            return self.db.execute("DELETE FROM eas_sync WHERE account=? AND device=? AND folder=?", (account, device, folder)).rowcount

        return await self._run(delete)

    async def eas_devices(self, account: str | None = None) -> list[tuple]:
        def read() -> list[tuple]:
            sql = (
                "SELECT g.account, g.device, g.type, g.model, g.os, g.created, g.last_seen, "
                "(SELECT group_concat(s.folder || ':' || s.counter, ',') FROM eas_sync s WHERE s.account=g.account AND s.device=g.device) "
                "FROM eas_device g"
            )
            if account:
                return self.db.execute(sql + " WHERE g.account=? ORDER BY g.last_seen DESC", (account,)).fetchall()
            return self.db.execute(sql + " ORDER BY g.account, g.last_seen DESC").fetchall()

        return await self._run(read)

    async def eas_folder_list(self, account: str, device: str) -> list[tuple[str, str, int]]:
        """Empty mail folders created by the client for this device: (folder, name, type)."""
        return await self._run(lambda: self.db.execute("SELECT folder,name,type FROM eas_folder WHERE account=? AND device=? ORDER BY folder", (account, device)).fetchall())

    async def eas_folder_new(self, account: str, device: str, folder: str, name: str, type: int) -> bool:
        def run() -> bool:
            if self.db.execute("SELECT count(*) FROM eas_folder WHERE account=? AND device=?", (account, device)).fetchone()[0] >= MAX_OWN_FOLDER:
                return False
            self.db.execute("INSERT OR IGNORE INTO eas_folder(account,device,folder,name,type,created) VALUES(?,?,?,?,?,?)", (account, device, folder, name, type, self._now()))
            return True

        return await self._run(run)

    async def eas_folder_gone(self, account: str, device: str, folder: str) -> bool:
        return await self._run(lambda: self.db.execute("DELETE FROM eas_folder WHERE account=? AND device=? AND folder=?", (account, device, folder)).rowcount > 0)

    async def eas_count_devices(self) -> int:
        return await self._run(lambda: self.db.execute("SELECT count(*) FROM eas_device").fetchone()[0])
