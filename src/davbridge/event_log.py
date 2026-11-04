"""JSON-lines event log with a field allow-list and a non-blocking writer (stdout and optional files).

Logged are operation, user, codes, count, duration and hashed identifiers - never event content, never secrets,
never `str(e)`. String values must match a narrow pattern, otherwise the field is dropped (and counted).
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Final, Sequence

if TYPE_CHECKING:
    from .clock import Clock

# ---------------------------------------------------------------------------------------------------------------------
# Public log vocabulary (English). Internal call sites still pass the pre-translation names; the tables below are the single
# place that maps them to the public names, so a later identifier translation does not change the log format.
# ---------------------------------------------------------------------------------------------------------------------

FIELD_NAMES: Final = {
    "kind": "event",
    "operation": "operation",
    "account": "user",
    "codes": "codes",
    "count": "count",
    "duration_ms": "duration_ms",
    "folder": "folder",
    "element": "item",
    "etag_hash": "etag_hash",
    "request_id": "request_id",
    "error_type": "error_type",
    "status": "status",
    "value": "value",
    "subscriptions": "subscriptions",
    "streams": "streams",
    "connection": "connection",
    "reason": "reason",
    "caldav_kind": "caldav_kind",
    "http_status": "http_status",
    "ip_hash": "ip_hash",
    "expiry_s": "backoff_seconds",
    "command": "command",
    "version": "version",
    "device": "device",
    "device_type": "device_type",
    "agent": "agent",
    "path": "path",
    "query": "query",
    "phase": "phase",
    "bytes_in": "bytes_in",
    "bytes_out": "bytes_out",
    "field": "field",
    "kind_mobile": "mobile_kind",
    "protocol_value": "protocol_value",
    "schema": "auth_scheme",
}
ALLOWED_KEYS: Final = frozenset(FIELD_NAMES.values())
ALLOWED_FIELDS: Final = ALLOWED_KEYS

SEVERITY: Final = {
    "debug": frozenset({"folder_access", "eas_field_ignored", "eas_change_skipped"}),
    "warning": frozenset(
        {
            "login_rejected",
            "change_rejected",
            "eas_conflict",
            "eas_sync_invalid",
            "eas_wbxml_error",
            "eas_caldav_error",
            "notifier_token_invalid",
            "notifier_auth",
            "notifier_error",
            "version_rejected",
            "eas_command_unknown",
        }
    ),
}
LEVELS: Final = ("debug", "info", "warning", "error")
_DEFAULT: Final = re.compile(r"[A-Za-z0-9:_.\-]{0,64}")
_PER_KEY: Final = {
    "item": re.compile(r"[0-9a-f]{12}"),
    "etag_hash": re.compile(r"[0-9a-f]{8}"),
    "ip_hash": re.compile(r"[0-9a-f]{8,16}"),
    "user": re.compile(r"[A-Za-z0-9._@+\-]{1,64}"),
    "command": re.compile(r"[A-Za-z]{1,32}"),
    "version": re.compile(r"[0-9.]{1,6}"),
    "device": re.compile(r"[0-9a-f]{12}"),
    "device_type": re.compile(r"[A-Za-z0-9]{1,40}"),
    "agent": re.compile(r"[A-Za-z][A-Za-z0-9._\-]{0,40}(/[0-9][A-Za-z0-9._\-]{0,20})?"),
    "path": re.compile(r"[A-Za-z0-9._\-]{1,40}(:[A-Za-z0-9._\-]{1,40}){0,3}"),
    "query": re.compile(r"[A-Za-z0-9,_\-]{0,64}"),
    "field": re.compile(r"[A-Za-z0-9:]{1,48}"),
    "protocol_value": re.compile(r"[A-Za-z0-9]{1,24}"),
}
MAX_LIST: Final = 20
MAX_TAG_BYTES: Final = 100 * 1024 * 1024


class FileStorage:
    """Writes lines in a thread of its own (never in the event loop). Paths only below `root`. Retention 30 days."""

    def __init__(self, root: Path | None, retention_days: int = 30, size: int = 10000, now: Callable[[], float] = time.time) -> None:
        self._root = Path(root).resolve() if root is not None else None
        if self._root is not None:
            self._root.mkdir(parents=True, exist_ok=True)
        self._days = retention_days
        self._now = now
        self._q: queue.Queue[tuple[Path | None, str, bool] | None] = queue.Queue(maxsize=size)
        self.lost = 0
        self.error = 0
        self._last_tag = ""
        self._tag_bytes = 0
        self._t = threading.Thread(target=self._run, name="log-storage", daemon=True)
        self._t.start()

    def write(self, path: Path | None, text: str, *, one_time: bool = False) -> None:
        """`pfad=None` writes the line to stdout instead of a file."""
        try:
            self._q.put_nowait((Path(path) if path is not None else None, text, one_time))
        except queue.Full:
            self.lost += 1

    def wait_empty(self) -> None:
        self._q.join()

    def close(self) -> None:
        self._q.put(None)
        self._t.join(timeout=5)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item is None:
                    return
                self._write(*item)
            except Exception:  # the storage must never disturb the service; only count
                self.error += 1
            finally:
                self._q.task_done()

    def _write(self, path: Path | None, text: str, one_time: bool) -> None:
        if path is None:
            try:
                sys.stdout.write(text + "\n")
                sys.stdout.flush()
            except (OSError, ValueError):
                self.error += 1
            return
        if self._root is None:
            return
        target = (path if path.is_absolute() else self._root / path).resolve()
        if self._root not in target.parents:
            self.error += 1
            return
        tag = datetime.fromtimestamp(self._now(), timezone.utc).strftime("%Y-%m-%d")
        if tag != self._last_tag:
            self._last_tag = tag
            self._tag_bytes = 0
            self._clean_up()
        if self._tag_bytes + len(text) > MAX_TAG_BYTES:
            self.lost += 1
            return
        self._tag_bytes += len(text) + 1
        target.parent.mkdir(parents=True, exist_ok=True)
        if one_time:
            if target.exists():
                return
            tmp = target.with_name(target.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            os.replace(tmp, target)
            return
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(text + "\n")

    def _clean_up(self) -> None:
        if self._root is None:
            return
        limit = self._now() - self._days * 86400
        for file in self._root.rglob("*"):
            try:
                if file.is_file() and file.suffix in (".jsonl", ".json", ".tmp") and file.stat().st_mtime < limit:
                    file.unlink()
            except OSError:
                self.error += 1


class EventLog:
    def __init__(self, directory: Path | None, clock: "Clock", storage: FileStorage, *, level: str = "info", stdout: bool = False) -> None:
        self._dir = Path(directory) if directory is not None else None
        self._clock = clock
        self._storage = storage
        self._level = LEVELS.index(level)
        self._stdout = stdout
        self.discarded = 0

    def _valid(self, key: str, value: object) -> bool:
        pattern = _PER_KEY.get(key, _DEFAULT)
        return isinstance(value, str) and pattern.fullmatch(value) is not None

    def _clean(self, fields: dict) -> dict:
        out: dict = {}
        for internal, v in fields.items():
            if v is None:
                continue
            k = FIELD_NAMES.get(internal)
            if k is None:
                self.discarded += 1
                continue
            if isinstance(v, bool) or isinstance(v, (int, float)):
                out[k] = v
            elif isinstance(v, str):
                if self._valid(k, v):
                    out[k] = v
                else:
                    self.discarded += 1
            elif isinstance(v, (list, tuple)):
                parts = [x for x in v[:MAX_LIST] if self._valid(k, x) or isinstance(x, (int, float)) and not isinstance(x, bool)]
                if len(parts) != len(v):
                    self.discarded += 1
                out[k] = parts
            else:
                self.discarded += 1
        return out

    def _severity_of(self, event: object, is_error: bool) -> int:
        if is_error:
            return 3
        for name, group in SEVERITY.items():
            if event in group:
                return LEVELS.index(name)
        return 1

    def _write(self, fields: dict, is_error: bool = False) -> None:
        clean = self._clean(fields)
        if self._severity_of(clean.get("event"), is_error) < self._level:
            return
        now = self._clock.now()
        moment = datetime.fromtimestamp(now, timezone.utc)
        text = json.dumps({"time": moment.isoformat(timespec="milliseconds"), **clean}, ensure_ascii=True, separators=(",", ":"))
        if self._dir is not None:
            self._storage.write(self._dir / f"davbridge-{moment:%Y-%m-%d}.jsonl", text)
        if self._stdout:
            self._storage.write(None, text)

    def request(self, op: str, account: str, codes: Sequence[str], count: int, duration_ms: int, folder: str | None = None, element: str | None = None, request_id: str | None = None) -> None:
        self._write(
            {"kind": "request", "operation": op, "account": account, "codes": list(codes), "count": count, "duration_ms": duration_ms, "folder": folder, "element": element, "request_id": request_id}
        )

    def event(self, kind: str, **fields: object) -> None:
        self._write({"kind": kind, **fields})

    def error(self, kind: str, exc: BaseException, **fields: object) -> None:
        """Log an exception: only the type name (never text or arguments)."""
        self._write({"kind": kind, "error_type": type(exc).__name__, **fields}, is_error=True)
