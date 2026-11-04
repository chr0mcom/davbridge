# Operations

Day-to-day running of DAVbridge. All `davbridge` commands run inside the container, for example
`docker compose exec davbridge davbridge check`. Commands that change state ask for confirmation or require an explicit flag.

> **After restoring the CalDAV server from a backup, you must reset DAVbridge's sync state and devices.**
> See [After a CalDAV server restore](#after-a-caldav-server-restore).

## Contents

[Logs](#logs) - [Check](#check) - [Backup](#backup) - [Restore](#restore) - [Upgrade](#upgrade) - [Rollback](#rollback) -
[Reset](#reset) - [Key rotation](#key-rotation) - [Unblock](#unblock) - [After a CalDAV server restore](#after-a-caldav-server-restore) -
[Monitoring](#monitoring) - [Removing a user or device](#removing-a-user-or-device)

## Logs

DAVbridge writes one structured **JSON object per line to stdout**. There is no HTTP access log. Read it with:

```bash
docker compose logs -f davbridge
docker compose logs --since 30m davbridge | grep '"reason"'
```

Logs contain operation or command names, user names, response codes, counts, durations, request IDs and hashed identifiers.
They contain **no passwords, event content, raw device IDs or request bodies**; fields come from an allow-list. Set the level with
`DAVBRIDGE_LOG_LEVEL` (`debug`, `info`, `warning`, `error`). Optional daily file logs (`davbridge-YYYY-MM-DD.jsonl`) are written below the directory in
`DAVBRIDGE_LOGS` (unset by default; the directory must be writable by UID 65532); they are not part of the state you must back up and files older than 30 days
are removed. Rotate Docker's own log files (`logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}`).
How to read them: [troubleshooting.md](troubleshooting.md#reading-the-structured-logs).

## Check

`davbridge check` is read-only and never prints secrets.

| Option | Effect |
|---|---|
| (none) | Configuration (strict validation, environment overrides, warnings for pattern mode, disabled TLS verification, plain HTTP, `follow_redirects`), EWS and EAS on/off and the mobile-cloud acknowledgement, state directory (create/rename/delete probe, owner and mode, free space), `hmac.key` (present, 32 bytes, mode), the SQLite database (integrity check, schema version, size, sync states, EAS devices, active lock-outs), version and dependency versions, CalDAV reachability (TLS verified, no redirect, 5xx, DAV capabilities) |
| `--user USER --password-stdin` | Sign-in of that user at the CalDAV server, then every configured calendar (exists, supports `VEVENT`, effective rights) or, for users with autodiscovery, the discovered candidates and the default calendar, without reading events. The password is read from standard input only. |
| `--password-stdin` alone | Standard input holds `user:password` lines; every user is tested and the report names the ones that fail |
| `--public` | Anonymous probes of `server.public_url`: `/EWS/Exchange.asmx` and `/Microsoft-Server-ActiveSync` must answer `401` with a plain Basic challenge (no Bearer, Negotiate or NTLM), unknown paths `404`, `/healthz` must not be routable, no redirect to a foreign host, TLS verified |
| `--json` | Versioned JSON report (`schema_version` 1: `result`, `exit_code`, `findings[]` with `id`, `level`, `message`) |
| `--show-effective-config` | Merged configuration after environment overrides |
| `--config PATH` | Check another configuration file |

Findings have the level `ok`, `info`, `warning` or `error`. Exit codes: `0` no warnings or errors, `1` warnings, `2` a problem that prevents operation. `check` never
changes the state directory (apart from a probe file that is deleted again), never prints a password and never reads events. A check from the host itself does not prove
reachability from the internet; DAVbridge runs no probe service and no telemetry. Not part of `check` in this version: DNS record listing, certificate expiry dates,
proxy buffering/compression detection and a streaming test; `davbridge diagnose` (below) shows service status and counters.

`davbridge diagnose [--short]` prints one status line of the *running* service (`status.json` age, CalDAV reachability, active lock-outs, subscriptions, sync states)
and exits `1` on a problem.

## Backup

What matters:

| File in `/var/lib/davbridge` | Importance |
|---|---|
| `hmac.key` | **Secret.** Loss changes all identifiers; every client must rebuild its view. |
| `davbridge.sqlite` (+ `-wal`, `-shm` while running) | Disposable but useful: sync states, devices, lock-outs. Loss = rebuild, load, possibly repeated client actions. No event content. |
| `status.json` | Not needed |

```bash
docker compose exec davbridge davbridge backup --output /var/lib/davbridge/backup/davbridge-$(date +%F).tar
```

`backup` uses the SQLite online-backup API (consistent while running, also with the database in WAL mode), refuses to overwrite an existing file unless
`--force` is given and writes atomically with mode `0600`. A missing target directory is created (one level only, mode `0700`; its parent must exist). On file systems without hard links (FAT, some network shares) the finished file is renamed into place after a last existence check. With a target ending in `.tar` the archive contains `davbridge.sqlite`, `hmac.key`, `davbridge.toml`
(the effective configuration at backup time, file plus environment overrides, no secrets), `manifest.json` (format, version, schema version, SHA-256 and size of every
file); `--without-key` leaves the key out. With any other target name only the SQLite database is written, as a single file without `-wal`/`-shm`; back up `hmac.key` separately.
**Treat an archive with the key like a password**: encrypt it and store it off-host. Never copy the `.sqlite` file alone while the service is running.
Your CalDAV server is the real data store; back that up separately.

Mount a host directory for `--output` (for example `./backup:/backup`, writable by UID 65532) if you want the archive outside the state volume.

## Restore

1. `docker compose down`.
2. Extract the archive into a scratch directory and verify the checksums listed in its manifest.
3. Put `hmac.key` and `davbridge.sqlite` into the state volume (owner `65532:65532`, mode `0600`; the service refuses a key with any other owner or looser mode).
4. Restore the configuration file from your own configuration management (the archive's copy is for reference).
5. `docker compose run --rm davbridge davbridge migrate --check`, then `docker compose up -d` and `davbridge check`.

If the CalDAV server was *not* restored at the same time, nothing else is needed. If it was, also see
[below](#after-a-caldav-server-restore).

## Upgrade

1. Read [CHANGELOG.md](../CHANGELOG.md) and any migration notes. Pin a version tag, or better the image digest from the release notes (never `latest`).
2. `davbridge check` on the running version, then a [backup](#backup).
3. Pull the new image and check the database schema without changing anything:
   `docker compose pull && docker compose run --rm davbridge davbridge migrate --check`.
   Exit `0`: the schema is current (or there is no database yet). Exit `1`: the schema is **older** (currently schema 3), and the new version
   **discards the database at its first start and creates an empty one** - there is no in-place migration. That costs sync states, EAS device records and
   lock-outs (clients resynchronize from CalDAV, no calendar data is lost); `hmac.key` and the configuration are never touched. Keep the backup from step 2.
   Exit `1` with "Refused" means the database is **newer** than this version (a rollback) or not a DAVbridge database at all: the service then refuses to start (exit code 2)
   and deletes nothing.
4. Recreate the service: `docker compose up -d` (stop grace 20 s). **Open EWS subscriptions are lost** - Outlook for Mac users
   may have to use *Synchronize* or restart Outlook; mobile users pull to refresh.
5. `davbridge check --public` and a client smoke test (create, change and delete a test event).
6. Before 1.0, a minor release can contain incompatible configuration, CLI or state changes; read the notes.

## Rollback

Roll back only if the release notes state that the previous version is schema-compatible. An older version that finds a newer database refuses to start (exit code 2) and leaves
the file untouched; restore the backup taken before the upgrade ([Restore](#restore)) with the old image. Reset the sync state if in doubt ([Reset](#reset)).

## Reset

Granular and destructive. `reset all` needs `--yes-i-understand`; the others act on exactly the user (and calendar or device) you name.

| Command | Effect |
|---|---|
| `davbridge reset sync --user USER [--calendar ID]` | Discard EWS sync states (the client rebuilds its calendar view) |
| `davbridge reset devices --user USER [--device HASH]` | Discard EAS device and sync states (the device starts again from sync key 0) |
| `davbridge reset all --yes-i-understand` | Discard the whole SQLite state. **Never** deletes `hmac.key` or configuration. |
| `davbridge devices [--user USER]` | List known EAS devices (hashed IDs, type, last seen) |

Resets cost a full resynchronization per client, not data. Duplicates are avoided by stable identifiers.

## Key rotation

`davbridge rotate-key --yes-i-understand` replaces `hmac.key` with a new random key (written atomically, mode `0600`). It is a separate, explicitly confirmed operation. The new key is written and the old one linked as a backup first; only then is the key-bound state discarded and the new key put in place, so a failure beforehand leaves the key and the state untouched.
**Consequences:** every identifier DAVbridge derived from the key changes - folder and item IDs, change keys, sync states, EAS sync keys, device hashes and the IDs of
discovered calendars. Therefore the command also deletes the state that is bound to the old key (sync states, EAS devices and their sync state, create de-duplication,
conflict anchors); lock-outs and stored Outlook settings stay. Outlook for Mac then performs a full resynchronization (restart Outlook or use *Synchronize*).
Outlook for iOS/Android resynchronizes its calendar and may have to be re-provisioned: if it keeps failing, remove and re-add the account.

Procedure: stop the service (`docker compose stop davbridge`; a clean shutdown removes `status.json`, and the command refuses to run while that file is younger than 150 seconds: after a crash wait as long as the message says, or add `--force` if you are sure that the service is stopped), run
`docker compose run --rm davbridge davbridge rotate-key --yes-i-understand`, start the service, verify with `davbridge check`, take a new backup. The previous key is kept next to the
new one as `hmac.key.<timestamp>.old` (also a secret; delete it once everything works, or use `--no-keep-old`); a backup taken before the rotation needs the old key. Do it after a
suspected key leak or on a defined schedule, outside working hours. Credentials (app passwords) are unaffected and are rotated on the CalDAV server:

1. Create a new app password; update the client; revoke the old one.
2. DAVbridge accepts up to four valid app passwords per user at once, so devices can be moved one at a time.

## Unblock

After repeated failed logins an IP address is locked (EWS and EAS counters are separate; EAS answers `503` with `Retry-After`). The lock-out table stores the **raw client IP
addresses** (see [privacy.md](privacy.md#client-ip-addresses-and-lock-outs)); `unblock` removes entries.

```bash
docker compose exec davbridge davbridge unblock --ip 203.0.113.7
docker compose exec davbridge davbridge unblock --all
```

Correct the client credentials first, otherwise the lock returns. If *everybody* is locked out, the proxy probably does not
pass the real client IP ([troubleshooting.md](troubleshooting.md#everybody-is-locked-out-or-503-for-all-clients)).

## After a CalDAV server restore

Restoring a CalDAV server can move calendar sync tokens **backwards**. Clients and DAVbridge would then believe they are up
to date while they are not. After every restore (or migration to a new CalDAV server):

```bash
docker compose exec davbridge davbridge reset sync --user alice      # EWS: each affected user
docker compose exec davbridge davbridge reset devices --user alice   # EAS: each affected user
```

Then ask users to synchronize; Outlook for Mac users may need to remove and re-add the account if the calendar looks stale.

## Monitoring

- Container health (`docker compose ps`; the built-in health check runs `davbridge healthcheck`, which probes the internal `/healthz` on the port of `server.listen`).
- `status.json` in the state directory: age should stay below a few minutes; also shows the last successful CalDAV contact,
  open subscriptions, active locks, EAS devices and ping count.
- A cron job running `davbridge check --json` and alerting on exit codes `1`/`2`.
- Certificate expiry of the public name (monitor it yourself; `check --public` fails on an invalid certificate chain but does not report the expiry date).

## Removing a user or device

1. Revoke the user's app password(s) on the CalDAV server.
2. Remove the user from `allowed_users`/`[[users]]` and restart.
3. `davbridge reset sync --user USER` and `davbridge reset devices --user USER`.
4. For Outlook mobile users, remember the copy at Microsoft ([privacy.md](privacy.md#revocation-and-removal)).
