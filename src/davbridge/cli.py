"""`davbridge` command line: operations on the local state directory and the configuration.

The commands run as the same user as the service, open SQLite themselves and never talk HTTP to the service. Exit codes:
0 success, 1 operational failure or refusal, 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import errno
import hashlib
import io
import json
import os
import re
import secrets
import sys
import tarfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import __version__, check, config_migration, toml_writer
from .clock import SystemClock
from .config import DEFAULT_STATE_DIR, Config, ConfigError, apply_environment, check_start, config_path, from_dict, listen_address, load, load_raw, public_representation, read_key
from .store import SCHEMA_VERSION, DatabaseError, DatabaseRefused, Store, Verdict, backup_database, inspect_database, refusal_message

DEVICE_HASH = re.compile(r"[0-9a-f]{12}")
USER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}")
CALENDAR_ID = re.compile(r"[a-z0-9-]{1,40}")
HMAC_KEY_FILE = "hmac.key"
DATABASE_FILE = "davbridge.sqlite"


PATTERN_WARNING = (
    'warning: auth.mode = "pattern": ANY CalDAV user whose name matches auth.username_pattern and who has a valid CalDAV password can use this bridge. '
    "Keep the pattern as narrow as possible and read docs/configuration.md."
)


class CliError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _state_dir() -> Path:
    return Path(os.environ.get("DAVBRIDGE_STATE_DIR", DEFAULT_STATE_DIR))


def _load_config(path_option: str | None) -> Config:
    path = Path(path_option) if path_option else config_path(os.environ)
    try:
        return load(path, os.environ)
    except ConfigError as e:
        raise CliError(str(e), 2) from None


async def _open_store() -> Store:
    path = _state_dir() / DATABASE_FILE
    if not path.is_file():
        raise CliError(f"no state database found at {path} (is DAVBRIDGE_STATE_DIR correct? the service creates it on first start)")
    try:
        return await Store.open(path, SystemClock())
    except DatabaseRefused as e:
        raise CliError(str(e), 2) from None
    except OSError as e:
        raise CliError(f"cannot open the state database at {path}: {e.strerror}") from None


def _check_user(value: str) -> str:
    if not USER_NAME.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid user name")
    return value


def _check_device(value: str) -> str:
    if not DEVICE_HASH.fullmatch(value):
        raise argparse.ArgumentTypeError("a device hash is 12 lower-case hexadecimal characters (see `davbridge devices`)")
    return value


def _check_calendar(value: str) -> str:
    if not CALENDAR_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("a calendar id consists of [a-z0-9-] (up to 40 characters)")
    return value


# ------------------------------------------------------------------ commands without state


def cmd_version(args: argparse.Namespace) -> int:
    commit = os.environ.get("GIT_COMMIT", "")
    print(f"davbridge {__version__}" + (f" (commit {commit[:12]})" if commit and commit != "unknown" else ""))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    state = _state_dir()
    target = state / HMAC_KEY_FILE
    try:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as e:
        raise CliError(f"cannot create the state directory {state}: {e.strerror}") from None
    if os.path.lexists(target):
        raise CliError(f"refusing to overwrite the existing key file {target}")
    temp = state / f".{HMAC_KEY_FILE}.tmp-{os.getpid()}"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(base64.b64encode(secrets.token_bytes(32)).decode("ascii") + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, target)
        except FileExistsError:
            raise CliError(f"refusing to overwrite the existing key file {target}") from None
    except OSError as e:
        raise CliError(f"cannot write the key file {target}: {e.strerror}") from None
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    print(f"Created {target} (mode 0600). It is a secret: include it in your backups and never commit it.")
    return 0


def cmd_config_validate(args: argparse.Namespace) -> int:
    path = Path(args.config) if args.config else config_path(os.environ)
    try:
        raw = load_raw(path)
        apply_environment(raw, os.environ)
        config = from_dict(raw, os.environ)
    except ConfigError as e:
        raise CliError(str(e), 2) from None
    print(f"Configuration OK: {path} ({len(config.accounts)} user(s), EWS {'on' if config.ews_enabled else 'off'}, EAS {'on' if config.eas.enabled else 'off'})")
    try:
        check_start(config)
    except ConfigError as e:
        print(f"warning: {e}; the service will refuse to start with this configuration", file=sys.stderr)
    if config.auth_mode == "pattern":
        print(PATTERN_WARNING, file=sys.stderr)
    if args.show_effective_config:
        print()
        print(toml_writer.dumps(public_representation(config), "Effective configuration (file + environment overrides)"), end="")
    return 0


def cmd_config_migrate(args: argparse.Namespace) -> int:
    try:
        result = config_migration.migrate_files(Path(args.source), Path(args.target), feature_profile=args.feature_profile, env=os.environ)
    except config_migration.MigrationError as e:
        raise CliError(str(e), 2) from None
    print(config_migration.format_report(result, Path(args.source), Path(args.target)))
    return 0


def _run_server(host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(
        "davbridge.main:app",
        host=host,
        port=port,
        workers=1,
        server_header=False,
        access_log=False,
        proxy_headers=False,
        timeout_keep_alive=75,
        timeout_graceful_shutdown=10,
    )


def cmd_serve(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    try:
        check_start(config)
    except ConfigError as e:
        raise CliError(str(e), 2) from None
    host, port = listen_address(config.listen)
    try:
        read_key(config.state_dir / HMAC_KEY_FILE)
    except ConfigError as e:
        raise CliError(str(e), 2) from None
    try:
        info = inspect_database(config.state_dir / DATABASE_FILE, SystemClock().now(), integrity=False)
    except (DatabaseError, OSError):
        raise CliError(f"{config.state_dir / DATABASE_FILE} is not a readable SQLite database. Refusing to touch it: restore a backup or move the file away yourself", 2) from None
    if info.verdict in (Verdict.NEWER, Verdict.FOREIGN):
        raise CliError(refusal_message(config.state_dir / DATABASE_FILE, info.verdict, info.schema_version), 2)
    if args.config:
        # The application reloads its configuration in the lifespan from DAVBRIDGE_CONFIG: it has to read the file that was just validated.
        os.environ["DAVBRIDGE_CONFIG"] = str(Path(args.config).resolve())
    _run_server(host, port)
    return 0


def cmd_healthcheck(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    host, port = listen_address(config.listen)
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1" if host == "0.0.0.0" else "::1"
    netloc = f"[{host}]" if ":" in host else host
    try:
        response = httpx.get(f"http://{netloc}:{port}/healthz", timeout=4, follow_redirects=False, trust_env=False)
    except httpx.HTTPError:
        print("unhealthy: the service does not answer", file=sys.stderr)
        return 1
    if response.status_code != 200:
        print(f"unhealthy: /healthz answered {response.status_code}", file=sys.stderr)
        return 1
    return 0


STATUS_FRESH_SECONDS = 150


def _status_age(state: Path) -> float | None:
    """Seconds since the service last wrote status.json (it rewrites the file every minute and removes it on a clean shutdown)."""
    try:
        return max(0.0, SystemClock().now() - (state / "status.json").stat().st_mtime)
    except OSError:
        return None


def _service_running(state: Path) -> bool:
    age = _status_age(state)
    return age is not None and age < STATUS_FRESH_SECONDS


def _create_private_file(directory: Path, name: str) -> Path:
    temp = directory / f".{name}.tmp-{os.getpid()}"
    try:
        os.close(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600))
    except OSError as e:
        raise CliError(f"cannot create a file in {directory}: {e.strerror}") from None
    return temp


LINK_UNSUPPORTED = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV}


def _prepare_directory(directory: Path) -> None:
    """The target directory of a backup: an existing directory, or one new level (mode 0700) below an existing one."""
    if directory.is_dir():
        return
    if os.path.lexists(directory) or not directory.parent.is_dir():
        raise CliError(f"the directory {directory} does not exist and its parent {directory.parent} is not an existing directory either")
    try:
        os.mkdir(directory, 0o700)
        os.chmod(directory, 0o700)
    except OSError as e:
        raise CliError(f"cannot create the directory {directory}: {e.strerror}") from None


def _publish(temp: Path, target: Path, force: bool) -> None:
    """Moves the finished temporary file into place without ever replacing an existing target unless `force` is given."""
    try:
        os.chmod(temp, 0o600)
        if force:
            os.replace(temp, target)
        else:
            try:
                os.link(temp, target)
            except FileExistsError:
                raise CliError(f"refusing to overwrite the existing file {target} (use --force)") from None
            except OSError as e:
                if e.errno not in LINK_UNSUPPORTED:
                    raise
                # No hard links on this file system (FAT, some network mounts): check once more and rename.
                if os.path.lexists(target):
                    raise CliError(f"refusing to overwrite the existing file {target} (use --force)") from None
                os.rename(temp, target)
                return
            os.unlink(temp)
    except OSError as e:
        raise CliError(f"cannot write {target}: {e.strerror}") from None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _tar_member(archive: tarfile.TarFile, name: str, data: bytes, now: float) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime, info.uid, info.gid, info.uname, info.gname = len(data), 0o600, int(now), 0, 0, "", ""
    archive.addfile(info, io.BytesIO(data))


def cmd_backup(args: argparse.Namespace) -> int:
    state = _state_dir()
    source = state / DATABASE_FILE
    if not source.is_file():
        raise CliError(f"no state database found at {source} (is DAVBRIDGE_STATE_DIR correct? the service creates it on first start)")
    target = Path(args.output)
    _prepare_directory(target.parent)
    if os.path.lexists(target) and not args.force:
        raise CliError(f"refusing to overwrite the existing file {target} (use --force)")
    as_tar = target.suffix == ".tar"
    temp = _create_private_file(target.parent, target.name)
    snapshot = _create_private_file(target.parent, target.name + ".db") if as_tar else temp
    try:
        try:
            integrity = backup_database(source, snapshot)
        except (DatabaseError, OSError) as e:
            raise CliError(f"cannot back up the state database: {getattr(e, 'strerror', None) or type(e).__name__}") from None
        if integrity != "ok":
            raise CliError(f"the backup failed its integrity check: {integrity[:200]}")
        if as_tar:
            _write_archive(temp, snapshot, state, with_key=not args.without_key)
        _publish(temp, target, args.force)
    finally:
        for leftover in {temp, snapshot}:
            try:
                os.unlink(leftover)
            except OSError:
                pass
    print(f"Wrote {target} ({target.stat().st_size} bytes, sha256 {_sha256(target)}, mode 0600).")
    if as_tar and not args.without_key:
        print("The archive contains hmac.key: protect it like a password.")
    else:
        print("The backup does not contain hmac.key. Without that key the restored database is useless; back it up separately (and keep it secret).")
    return 0


def _write_archive(temp: Path, snapshot: Path, state: Path, *, with_key: bool) -> None:
    now = SystemClock().now()
    files: dict[str, bytes] = {DATABASE_FILE: snapshot.read_bytes()}
    key_path = state / HMAC_KEY_FILE
    if with_key:
        try:
            files[HMAC_KEY_FILE] = key_path.read_bytes()
        except OSError as e:
            raise CliError(f"cannot read {key_path}: {e.strerror} (use --without-key for a database-only archive)") from None
    try:
        config = load(config_path(os.environ), os.environ)
        files["davbridge.toml"] = toml_writer.dumps(public_representation(config), "Effective configuration at backup time (file + environment overrides)").encode("utf-8")
    except ConfigError:
        pass
    manifest = {
        "format": 1,
        "davbridge_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "created": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in sorted(files.items())},
    }
    with tarfile.open(temp, "w") as archive:
        for name, data in sorted(files.items()):
            _tar_member(archive, name, data, now)
        _tar_member(archive, "manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"), now)


def cmd_migrate(args: argparse.Namespace) -> int:
    path = _state_dir() / DATABASE_FILE
    try:
        info = inspect_database(path, SystemClock().now())
    except (DatabaseError, OSError) as e:
        raise CliError(f"cannot read the state database {path} ({getattr(e, 'strerror', None) or type(e).__name__})") from None
    if not info.exists:
        print(f"No state database at {path} yet: it is created with schema {SCHEMA_VERSION} at the first start. Nothing to migrate.")
        return 0
    if info.integrity != "ok":
        print(f"The integrity check of {path} failed ({info.integrity[:200]}). Restore a backup or discard the database with `davbridge reset all --yes-i-understand`.", file=sys.stderr)
        return 1
    if info.verdict in (Verdict.NEWER, Verdict.FOREIGN):
        print(f"Refused: {refusal_message(path, info.verdict, info.schema_version)}.", file=sys.stderr)
        return 1
    if info.verdict in (Verdict.CURRENT, Verdict.NEW):
        print(f"The state database uses schema {info.schema_version or 'none (empty)'}; this version needs schema {SCHEMA_VERSION}. No migration needed.")
        return 0
    if info.verdict == Verdict.ADOPT:
        print(f"The state database uses schema {SCHEMA_VERSION} but carries no DAVbridge application id yet; the service adds it at the next start. No migration needed, nothing is lost.")
        return 0
    print(
        f"The state database uses schema {info.schema_version or 'none (empty)'}, which is older than schema {SCHEMA_VERSION} of this version.\n"
        f"This version does not migrate in place: at the next start it DISCARDS the database and creates a new one.\n"
        f"Lost: {info.sync_states} sync state(s), {info.devices} EAS device record(s) and all lock-outs. Clients resynchronise from CalDAV; no calendar data is lost.\n"
        "hmac.key and the configuration are not touched. To keep the old file, run `davbridge backup` before starting the new version."
    )
    return 1


ROTATE_WARNING = (
    "Rotating hmac.key invalidates every identifier DAVbridge ever handed out: folder and item ids, change keys, sync states, EAS sync keys and device "
    "records. Outlook for Mac performs a full resynchronisation (restart Outlook or use Sync). Outlook for iOS/Android resynchronises its calendar and may "
    "have to be re-provisioned; remove and re-add the account if it keeps failing. Stop the service first and start it again afterwards."
)


async def cmd_rotate_key(args: argparse.Namespace) -> int:
    state = _state_dir()
    if not args.yes_i_understand:
        raise CliError(f"refusing to rotate the key without --yes-i-understand.\n{ROTATE_WARNING}", 2)
    key_path = state / HMAC_KEY_FILE
    if not key_path.is_file():
        raise CliError(f"no key file found at {key_path}; create one with `davbridge init`")
    age = _status_age(state)
    if not args.force and age is not None and age < STATUS_FRESH_SECONDS:
        wait = int(STATUS_FRESH_SECONDS - age) + 1
        raise CliError(
            f"the service looks like it is running (status.json was written {int(age)} seconds ago). Stop it first. A service that was stopped cleanly removes status.json; "
            f"after a crash or a forced stop wait about {wait} more seconds, or use --force if you are sure that the service is not running"
        )
    temp = state / f".{HMAC_KEY_FILE}.tmp-{os.getpid()}"
    kept: Path | None = None
    discarded = 0
    published = False
    try:
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(base64.b64encode(secrets.token_bytes(32)).decode("ascii") + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if not args.no_keep_old:
                kept = state / f"{HMAC_KEY_FILE}.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.old"
                os.link(key_path, kept)
        except OSError as e:
            raise CliError(f"cannot prepare the new key file in {state}: {e.strerror}; nothing was changed") from None

        def publish() -> None:
            nonlocal published
            os.replace(temp, key_path)
            published = True

        try:
            if (state / DATABASE_FILE).is_file():
                store = await _open_store()
                try:
                    discarded = await store.discard_key_bound(publish)
                finally:
                    await store.close()
            else:
                publish()
        except OSError as e:
            backup = f" The backup {kept} was kept." if kept is not None else ""
            raise CliError(f"cannot write the new key file {key_path}: {e.strerror}. The old key is still in place and the state was not touched.{backup}") from None
        except DatabaseError as e:
            if published:
                raise CliError(f"the new key was installed but the key-bound state could not be discarded ({e}); run `davbridge reset all --yes-i-understand`") from None
            raise CliError(f"cannot discard the key-bound state ({e}); the old key is still in place") from None
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    print(f"Rotated {key_path} (mode 0600); discarded {discarded} key-bound state row(s).")
    if kept is not None:
        print(f"The previous key was kept as {kept}. Delete it once the service works again (it is a secret as well), or restore it together with a backup of the old state.")
    print(ROTATE_WARNING)
    return 0


def _read_passwords(args: argparse.Namespace) -> dict[str, str]:
    """Passwords are only accepted on standard input: one password (with --user) or `user:password` lines (without)."""
    if args.user and not args.password_stdin:
        raise CliError("--user needs --password-stdin: passwords are never accepted as arguments or environment variables", 2)
    if not args.password_stdin:
        return {}
    lines = [line.rstrip("\r\n") for line in sys.stdin.read().splitlines() if line.strip()]
    if args.user:
        if len(lines) != 1 or not lines[0]:
            raise CliError("with --user, standard input must contain exactly one line: the password", 2)
        return {args.user: lines[0]}
    result: dict[str, str] = {}
    for line in lines:
        name, separator, password = line.partition(":")
        if not separator or not name or not password or not USER_NAME.fullmatch(name):
            raise CliError("without --user, every line of standard input must have the form user:password", 2)
        result[name] = password
    if not result:
        raise CliError("standard input is empty: expected the password (with --user) or user:password lines", 2)
    return result


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.config) if args.config else config_path(os.environ)
    options = check.CheckOptions(path, os.environ, _read_passwords(args), args.public, args.show_effective_config, getattr(args, "transport", None))
    report = asyncio.run(check.run_check(options))
    if args.json:
        print(json.dumps(report.as_json(), indent=2))
    else:
        print(report.as_text())
        if report.effective_config is not None:
            print()
            print(toml_writer.dumps(report.effective_config, "Effective configuration (file + environment overrides)"), end="")
    return report.exit_code


# ------------------------------------------------------------------ state commands


def _utc(timestamp: int) -> str:
    return f"{datetime.fromtimestamp(timestamp, timezone.utc):%Y-%m-%d %H:%M}Z"


async def cmd_devices(args: argparse.Namespace) -> int:
    store = await _open_store()
    try:
        rows = await store.eas_devices(args.user)
    finally:
        await store.close()
    if not rows:
        print("No devices.")
        return 0
    print(f"{'USER':<20} {'DEVICE':<12} {'TYPE':<14} {'MODEL':<16} {'OS':<14} {'FIRST SEEN':<18} {'LAST SEEN':<18} FOLDERS")
    for user, device, kind, model, os_name, created, last, folders in rows:
        print(f"{user:<20} {device:<12} {kind:<14} {model or '-':<16} {os_name or '-':<14} {_utc(created):<18} {_utc(last):<18} {folders or '-'}")
    return 0


async def cmd_reset_sync(args: argparse.Namespace) -> int:
    store = await _open_store()
    try:
        count = await store.discard_states(args.user, args.calendar)
    finally:
        await store.close()
    print(f"Discarded {count} EWS sync state(s) for {args.user}" + (f", calendar {args.calendar}" if args.calendar else "") + ".")
    return 0


async def cmd_reset_devices(args: argparse.Namespace) -> int:
    store = await _open_store()
    try:
        count = await store.eas_delete(args.user, args.device)
    finally:
        await store.close()
    print(f"Discarded {count} EAS device/sync record(s) for {args.user}" + (f", device {args.device}" if args.device else "") + ".")
    return 0


async def cmd_reset_all(args: argparse.Namespace) -> int:
    if not args.yes_i_understand:
        raise CliError("refusing to discard all state without --yes-i-understand (hmac.key and the configuration are never touched)", 2)
    store = await _open_store()
    try:
        count = await store.discard_all()
    finally:
        await store.close()
    print(f"Discarded {count} state row(s). hmac.key and the configuration were not touched; every client will resynchronize.")
    return 0


async def cmd_unblock(args: argparse.Namespace) -> int:
    store = await _open_store()
    try:
        if args.all:
            count = await store.lock_lift(None)
        else:
            count = await store.lock_lift(args.ip) + await store.lock_lift(f"eas:{args.ip}")
    finally:
        await store.close()
    print(f"Removed {count} lock entry(ies).")
    return 0


async def cmd_diagnosis(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    status: dict = {}
    path = config.state_dir / "status.json"
    try:
        status = json.loads(path.read_text())
    except (OSError, ValueError):
        status = {}
    store = await _open_store()
    try:
        locks = await store.locks_active()
        sync_states = await store.count_states()
    finally:
        await store.close()
    age = None if not status else int(SystemClock().now() - status.get("time", 0))
    try:
        async with httpx.AsyncClient(timeout=5, verify=config.verify_tls, follow_redirects=False) as client:
            response = await client.options(config.caldav_base_url)
        caldav = "reachable" if response.status_code < 500 else "degraded"
    except httpx.HTTPError:
        caldav = "unreachable"
    ok = bool(status) and age is not None and age < 300 and caldav == "reachable"
    print(
        f"status={'ok' if ok else 'problem'} version={__version__} commit={config.commit} caldav={caldav} locks={locks} "
        f"subscriptions={status.get('subscriptions', '?')} streams={status.get('streams', '?')} sync_states={sync_states} status_age_seconds={age}"
    )
    if status and not args.short:
        print(json.dumps(status, indent=1, sort_keys=True))
    return 0 if ok else 1


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="davbridge",
        description="DAVbridge: calendar bridge between Outlook (EWS, Exchange ActiveSync 14.1) and a CalDAV server. "
        "Commands operate on the state directory (DAVBRIDGE_STATE_DIR, default /var/lib/davbridge) and the configuration "
        "(DAVBRIDGE_CONFIG, default /etc/davbridge/davbridge.toml).",
        epilog="Exit codes: 0 success, 1 operational failure or refusal, 2 usage or configuration error.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    check_parser = sub.add_parser(
        "check",
        help="read-only diagnosis: configuration, state directory, CalDAV server, optional sign-in and calendar test (exit 0 ok, 1 warning, 2 error)",
    )
    check_parser.add_argument("--config", metavar="PATH", help="configuration file (default: DAVBRIDGE_CONFIG)")
    check_parser.add_argument("--user", type=_check_user, help="test sign-in and calendars of this user; the password is read from standard input")
    check_parser.add_argument("--password-stdin", action="store_true", help="read the CalDAV (app) password from standard input; without --user: lines of user:password for several users")
    check_parser.add_argument("--public", action="store_true", help="also probe the public URL (401 with a Basic challenge, unknown paths, health endpoint)")
    check_parser.add_argument("--json", action="store_true", help="machine-readable output (schema_version 1)")
    check_parser.add_argument("--show-effective-config", action="store_true", help="include the merged configuration (file + environment overrides); it contains no secrets")
    check_parser.set_defaults(run=cmd_check)
    backup = sub.add_parser("backup", help="consistent online backup of the state database (SQLite backup API, mode 0600); a .tar target also bundles hmac.key, a manifest and checksums")
    backup.add_argument("--output", required=True, metavar="PATH", help="target file; with the suffix .tar an archive is written (database, hmac.key, effective config, manifest)")
    backup.add_argument("--force", action="store_true", help="replace an existing target (atomically)")
    backup.add_argument("--without-key", action="store_true", help="leave hmac.key out of a .tar archive")
    backup.set_defaults(run=cmd_backup)
    migrate_db = sub.add_parser("migrate", help="state database schema (use --check)")
    migrate_db.add_argument(
        "--check", action="store_true", required=True, help="report whether the database schema needs migration or would be discarded; changes nothing (exit 0 current, 1 action needed)"
    )
    migrate_db.set_defaults(run=cmd_migrate)
    rotate = sub.add_parser("rotate-key", help="replace hmac.key (invalidates all ids, sync states and devices; stop the service first)")
    rotate.add_argument("--yes-i-understand", action="store_true", help="confirm the consequences: every client resynchronises")
    rotate.add_argument("--force", action="store_true", help="do not refuse because status.json is recent (use only if you know the service is stopped)")
    rotate.add_argument("--no-keep-old", action="store_true", help="do not keep a copy of the previous key next to the new one")
    rotate.set_defaults(run=cmd_rotate_key)
    sub.add_parser("version", help="print the version").set_defaults(run=cmd_version)
    serve = sub.add_parser("serve", help="run the service (one uvicorn worker on server.listen); this is what the container starts")
    serve.add_argument("--config", metavar="PATH", help="configuration file (default: DAVBRIDGE_CONFIG)")
    serve.set_defaults(run=cmd_serve)
    health = sub.add_parser("healthcheck", help="probe /healthz on the configured listen address (exit 0 healthy, 1 unhealthy; used by the container health check)")
    health.add_argument("--config", metavar="PATH", help="configuration file (default: DAVBRIDGE_CONFIG)")
    health.set_defaults(run=cmd_healthcheck)
    sub.add_parser("init", help="create hmac.key in the state directory (never overwrites an existing key)").set_defaults(run=cmd_init)

    devices = sub.add_parser("devices", help="list known EAS devices (hashed identifiers only)")
    devices.add_argument("--user", type=_check_user, help="only this user")
    devices.set_defaults(run=cmd_devices)

    reset = sub.add_parser("reset", help="discard state (destructive; clients resynchronize)")
    reset_sub = reset.add_subparsers(dest="target", required=True, metavar="TARGET")
    rs = reset_sub.add_parser("sync", help="discard EWS sync states of a user")
    rs.add_argument("--user", required=True, type=_check_user)
    rs.add_argument("--calendar", type=_check_calendar, metavar="ID", help="only this calendar id")
    rs.set_defaults(run=cmd_reset_sync)
    rd = reset_sub.add_parser("devices", help="discard EAS devices and sync states of a user")
    rd.add_argument("--user", required=True, type=_check_user)
    rd.add_argument("--device", type=_check_device, metavar="HASH", help="only this device (see `davbridge devices`)")
    rd.set_defaults(run=cmd_reset_devices)
    ra = reset_sub.add_parser("all", help="discard the whole SQLite state; hmac.key and the configuration are never deleted")
    ra.add_argument("--yes-i-understand", action="store_true", help="confirm that all sync states, devices and lock-outs are discarded")
    ra.set_defaults(run=cmd_reset_all)

    unblock = sub.add_parser("unblock", help="remove login lock-outs")
    group = unblock.add_mutually_exclusive_group(required=True)
    group.add_argument("--ip", metavar="ADDRESS", help="unblock one client address (EWS and EAS)")
    group.add_argument("--all", action="store_true", help="remove all lock-outs")
    unblock.set_defaults(run=cmd_unblock)

    diagnosis = sub.add_parser("diagnose", help="print service status, CalDAV reachability and counters (exit 1 on a problem)")
    diagnosis.add_argument("--config", metavar="PATH", help="configuration file (default: DAVBRIDGE_CONFIG)")
    diagnosis.add_argument("--short", action="store_true", help="only the summary line")
    diagnosis.set_defaults(run=cmd_diagnosis)

    config = sub.add_parser("config", help="configuration tools")
    config_sub = config.add_subparsers(dest="config_command", required=True, metavar="SUBCOMMAND")
    validate = config_sub.add_parser("validate", help="validate a configuration file (strict) without starting the service")
    validate.add_argument("--config", metavar="PATH", help="configuration file (default: DAVBRIDGE_CONFIG)")
    validate.add_argument("--show-effective-config", action="store_true", help="print the merged configuration (file + environment overrides)")
    validate.set_defaults(run=cmd_config_validate)
    migrate = config_sub.add_parser("migrate", help="convert a legacy configuration file offline (never overwrites, writes mode 0600)")
    migrate.add_argument("--from", dest="source", required=True, metavar="OLD", help="legacy configuration file")
    migrate.add_argument("--to", dest="target", required=True, metavar="NEW", help="new configuration file (must not exist)")
    migrate.add_argument(
        "--feature-profile",
        choices=sorted(config_migration.FEATURE_PROFILES),
        help="required for legacy rollout stages other than M2: 'default' = all event fields, 'basic' = without categories and sensitivity",
    )
    migrate.set_defaults(run=cmd_config_migrate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.run(args)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        return int(result)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    raise SystemExit(main())
