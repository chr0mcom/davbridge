# Architecture

This document describes how DAVbridge is built, which rules keep it correct and safe, and what
its threat model is. It is written for operators who want to understand the behavior, and for contributors.

## 1. What DAVbridge is

DAVbridge is a small web service that sits between Outlook clients and a CalDAV server. To
Outlook it presents a **limited calendar-only facade** of two Microsoft protocols; to the CalDAV
server it behaves like a normal CalDAV client of the signed-in user.

```text
Outlook for Mac                      DAVbridge                         CalDAV server
(EWS, Autodiscover)   --HTTPS-->  reverse proxy --HTTP--> +------------------+
                                                          | EWS facade       |
Outlook for iOS/Android                                   | EAS 14.1 facade  |  --HTTPS-->  Nextcloud /
(via Microsoft cloud)  --HTTPS-->  reverse proxy --HTTP-->| mapping, state   |              sabre/dav ...
                                                          +------------------+
```

It is **not** an Exchange server, a mail server, or a Nextcloud app. It stores no calendar content of
its own.

## 2. Design rules

| # | Rule | Why |
|---|---|---|
| 1 | **CalDAV is the only source of truth.** DAVbridge persists no event content. | One authoritative copy; nothing to leak or to get out of sync. |
| 2 | **One process, one event loop, exactly one uvicorn worker.** No gunicorn, no reload, no replicas. | EWS subscriptions, EAS wake-ups, lock-outs and the credential cache live in memory. |
| 3 | **No blocking I/O on the event loop.** No synchronous network calls, no `time.sleep`, no SQLite access in handlers; bulk parsing runs in worker threads. | Long-lived streams and pings must never starve other requests. |
| 4 | **Conditional writes.** Every update and delete sends `If-Match` with the ETag the *client last saw*, never a freshly read one. | A concurrent change on the CalDAV side is never silently overwritten. |
| 5 | **Patch, do not rebuild.** Existing iCalendar objects are patched in place; unknown properties, extra alarms and `VTIMEZONE` blocks survive. | Other clients' data is not damaged by an Outlook edit. |
| 6 | **Closed write world.** Only tested fields are written; unknown fields are rejected or ignored in a defined way, never guessed. | Predictable behavior beats "approximately right". |
| 7 | **One visibility filter.** Find / Get / Sync / Fetch / estimate all use the same "can this event be shown and edited" decision. | Prevents events that appear in one operation and vanish in another. |
| 8 | **Signed identifiers.** All IDs sent to clients are HMAC-derived and bound to user, calendar and object type. | Clients cannot forge or transplant IDs. |
| 9 | **Notifications are hints.** EWS subscriptions and EAS pings are RAM state; correctness depends only on sync state and CalDAV. | A restart loses hints, never data. |
| 10 | **Disposable, bounded state.** SQLite holds bounded metadata with TTL and cleanup; no unbounded response caches. | Predictable disk usage; safe to reset. |
| 11 | **Allow-listed output.** Logs and diagnostics are built from allow-lists. | New fields cannot leak credentials or content by accident. |
| 12 | **Secure defaults.** `allowlist` authentication, no trusted proxies, EAS off, explicit calendars. | A fresh install exposes the least. |

Rules 2, 3 and the module boundaries in section 3 are enforced by architecture tests.

## 3. Components

Source layout (`src/davbridge/`). Dependencies point downwards; pure modules perform no I/O.

| Layer | Modules | Responsibility |
|---|---|---|
| Entry points | `main.py`, `cli.py`, `check.py` | ASGI app (routes, lifespan, error frame, status file, signal handling); the `davbridge` command (`serve` starts uvicorn with one worker on `server.listen`); the read-only `check` diagnosis |
| Protocol: EWS | `soap.py`, `operations/{folders,items,sync,notifications,auxiliary,autodiscover}.py` | SOAP parsing and rendering, one handler per EWS operation, POX Autodiscover |
| Protocol: EAS | `eas/{http,request,devices,wakeup,skeleton,mobile_autodiscover}.py`, `eas/commands/{sync,folders,ping,settings,items,provision,rejected}.py` | Request parsing, command dispatch, per-device sync state, mobile Autodiscover |
| Encoding | `eas/{wbxml,tags,tags_table}.py` | Hardened WBXML codec and token tables |
| Mapping (pure) | `ical.py`, `eas/calendar.py`, `eas/recurrence.py`, `eas/timezone.py`, `timezones.py`, `identifiers.py`, `permissions.py`, `calendar_info.py` | EWS/EAS field mapping to and from iCalendar, recurrence, time zones, HMAC identifiers, effective rights |
| Services | `auth.py`, `discovery.py`, `subscriptions.py`, `notifier.py`, `locks.py`, `clock.py` | Basic authentication and lock-out, calendar autodiscovery, EWS subscriptions, CalDAV change polling, per-object write locks |
| Backends | `caldav.py`, `store.py` | The only HTTP client (CalDAV) and the only SQLite access |
| Edge | `proxy_headers.py` | ASGI middleware: honors `X-Forwarded-For` / `X-Forwarded-Proto` only from `server.trusted_proxies` |
| Configuration tools | `config_migration.py`, `toml_writer.py` | Offline converter for legacy configuration files; minimal TOML writer for generated files |
| Cross-cutting | `config.py`, `errors.py`, `event_log.py`, `protocol_capture.py` | Validated configuration, error mapping, allow-listed JSON logging, structure-only protocol capture (only for accounts flagged as test accounts in code; no configuration option enables it) |

Enforced import rules: HTTP only in `caldav.py` (plus the entry points `main.py`, `cli.py` and `check.py`, which create clients); SQLite only in `store.py`; pure modules import no
I/O libraries; operation modules do not import each other; EAS command modules do not import each other.

### Request flow

1. The reverse proxy forwards one of the public paths (see [reverse-proxy.md](reverse-proxy.md)).
2. `auth.py` checks the lock-out state of the client IP, parses the `Basic` header, checks the user name against
   the allow-list (or the pattern), and verifies the password **against the CalDAV server**. Unknown users never
   cause a CalDAV request.
3. The protocol handler parses the request with hard limits (see below), consults the sync state in the
   store, talks to CalDAV through `caldav.py` and maps the result with the pure mapping modules.
4. The response is rendered; one allow-listed log record is written.

## 4. State

| Where | What | Lost on restart? |
|---|---|---|
| `/var/lib/davbridge/hmac.key` | Secret key for all HMAC identifiers. **Treat like a password.** | No (file) |
| `/var/lib/davbridge/davbridge.sqlite` (+ WAL/SHM), mode `0600` | Sync states, device records, lock-out counters (raw client IPs, see [privacy.md](privacy.md#client-ip-addresses-and-lock-outs)), de-duplication data, user configuration objects, conflict anchors | No (file) |
| `/var/lib/davbridge/status.json` | Small health summary refreshed about once a minute | No (file) |
| RAM | EWS subscriptions, EAS ping waiters, verified-credential cache (positive and negative), calendar metadata cache (5 minutes) | **Yes** |

The SQLite file contains **no event content** (titles, locations, attendees, bodies). It holds identifiers
(hashed), ETags and sync tokens. Losing it forces clients to rebuild their view, which costs time and CalDAV load but not data.
Losing `hmac.key` changes all identifiers and has the same effect. See [operations.md](operations.md) for backup and reset.

Tables (English names): `sync_state` (EWS sync states), `user_configuration` (EWS user configuration objects), `create_dedup` (short-lived create de-duplication), `lockout` (lock-out counters per
client address), `event_log` (conflict anchors: the ETag hash last delivered or written per item, 30 days), `eas_device`, `eas_sync` and `eas_folder` (EAS devices, their sync state and their compatibility folders).

The database carries the DAVbridge application id (`PRAGMA application_id` = `0x44415642`, "DAVB") and a schema version (`PRAGMA user_version`, currently **3**; the table and column names
are English). There is no in-place migration, and the start-up rules never destroy anything that is not an older DAVbridge database: a file with a **newer** schema (a rollback after an upgrade)
or a **foreign** file (another application id, or tables that are not ours) makes the service exit with code 2 and a message, and nothing is deleted or modified; an **older** schema (or a
file of ours without any version) is discarded and recreated at the next start; a file of the current schema without application id gets the id added. A file without the
application id counts as ours only if its tables and their columns match the DAVbridge schema exactly (with or without the EAS tables); two matching table names are not enough. `davbridge migrate --check` and
`davbridge check` announce each outcome beforehand. Because the database is disposable, no compatibility with databases of
the originating private deployment exists (the identifiers use a different HMAC context as well).

### Protocol capture (developer feature)

For accounts that are flagged as test accounts **in code** DAVbridge can record the *structure* of requests (element names, enumeration values, never text values). No configuration option
enables this in v0.1, so a normal installation never writes these files. They go below `capture/` in the directory of `DAVBRIDGE_LOGS` (state directory if unset):

| File | Content (JSON keys) |
|---|---|
| `capture/YYYY-MM-DD.jsonl` (EWS) | one line per request: `time`, `operation`, `fingerprint`, `codes`, `duration_ms`, `gap_s` (seconds since the previous request of the same user and operation) |
| `capture/eas-YYYY-MM-DD.jsonl` (EAS) | `time`, `command`, `status`, `codes`, `header` (header names, values only for an allow-list; never `Authorization`, cookies or forwarding headers), `request`, `response` (structure lines) |
| `capture/skeletons/<fingerprint>.json` | once per distinct EWS request structure: `operation`, `fingerprint`, `header`, `tree` (nested objects with `name`, `attribute`, `children`) |

## 5. Consistency and conflicts

- **EWS (Outlook for Mac).** Outlook does not send a change key on updates and deletes. DAVbridge therefore uses
  the ETag it **last delivered to that client** for the object as the conditional-write anchor. If the CalDAV copy has
  changed since, the operation is refused with `ErrorIrresolvableConflict` and the CalDAV version is kept;
  Outlook shows no error and receives the CalDAV version with its next synchronization.
- **EAS (Outlook mobile).** The CalDAV version wins. A conflicting change is answered with status 7 plus the server's
  version in the same response. Two loop-breakers keep clients from retrying forever: an identical, repeatedly rejected
  request is rejected again a limited number of times (for deletions the third identical request is executed), and a
  **delete brake** limits executed client deletions to `max(delete_brake_minimum, 10 % of known items)` per user and
  hour.
- **EAS partial updates.** In EAS 14.1 a missing, non-ghosted property in a `Change` means "clear it". DAVbridge applies this
  only to full-image changes (start and end present), so a delta-style change from a newer client can never delete fields.
- **Write-then-read.** After each successful write the new ETag is recorded as "known to the client".
- **Restores.** If the CalDAV server is restored from a backup, sync tokens can jump backwards; reset the sync state
  ([operations.md](operations.md#after-a-caldav-server-restore)).

## 6. Notifications

- **EWS.** Outlook subscribes with `Subscribe` and opens `GetStreamingEvents`. A background poller watches the CalDAV
  server (using `sync-collection` tokens) for users that currently have a subscription. On a change the event is written to the open
  stream, followed by a `Closed` status, so the client reconnects immediately. Keep-alive messages are sent regularly;
  a connection lasts at most `protocols.ews.stream_max_seconds` (default 1800 s). Pull subscriptions (`GetEvents`)
  are supported as well. Subscriptions live in RAM only.
  Outlook for Mac does **not** reliably fetch the calendar on such an event; see [limitations.md](limitations.md).
- **EAS.** `Ping` (and `Sync` with `Wait`) is answered when a CalDAV check shows a visible change, or when the heartbeat
  interval ends. Waiting costs no thread. A ping never replaces a sync; it only tells the client to run one.

## 7. Calendar discovery

By default each user's calendars are listed explicitly in the configuration (no exploration of the CalDAV server).
The optional `auto` mode (and pattern admission) performs a narrowly bounded discovery after a successful login (`discovery.py`, with the PROPFIND client in `caldav.py`): principal ->
calendar home -> one `PROPFIND Depth: 1`. It accepts only collections of type calendar with read access that support
`VEVENT`, excludes shared calendars unless `include_shared = true`, takes write permission only from
`current-user-privilege-set`, rejects every redirect, other host, port or scheme, and every path outside the canonical
calendar home, and caches metadata (never event content) for five minutes. Details in [configuration.md](configuration.md#calendar-discovery).

## 8. Threat model

Assets: calendar data, CalDAV credentials (app passwords), `hmac.key`, availability of the CalDAV server.

| Threat | Mitigation |
|---|---|
| Internet scanners and credential stuffing against the public endpoints | Allow-list of users (unknown users never reach CalDAV), per-IP lock-out with growing duration (`failure_limit`, `initial_lock_seconds`), separate EWS and EAS lock namespaces, per-user throttling of unknown passwords on EAS, negative cache |
| Spoofed `X-Forwarded-For` to evade or abuse lock-outs | Forwarded headers are honored **only** from `server.trusted_proxies` (default: none) |
| Stolen client credentials | Use per-device **app passwords**; revoke on the CalDAV server (takes effect immediately, positive cache is at most `positive_cache_seconds`) |
| Malicious or malformed XML / WBXML | DTDs and external entities forbidden, request size limit 1 MiB, nesting depth and node count limits, fuzz and bomb tests |
| SSRF / traversal through CalDAV discovery | Origin, port and path must stay inside the canonical calendar home; redirects are never followed; hrefs are normalized and decoded exactly once |
| Credentials or content in logs and diagnostics | Allow-listed fields with strict patterns; passwords, event content, raw device IDs and raw request bodies are never logged; no telemetry |
| Data loss through a misbehaving client | Conditional writes, delete brake, closed write world, lossless patching |
| Compromise of the container | Non-root UID 65532, read-only root file system, all capabilities dropped, no Docker socket, memory and PID limits |
| Exposure of internal endpoints | `/healthz` is not routed publicly; there is no admin HTTP interface |
| Malicious CalDAV server | **Out of scope.** The CalDAV server is trusted. |
| Compromise of the reverse proxy host | **Out of scope.** The proxy sees credentials in transit. |

Non-goals: protecting calendar data from Microsoft once Outlook mobile has been connected
([privacy.md](privacy.md)), multi-tenant hosting, high availability.

## 9. Failure behavior

| Situation | Behavior |
|---|---|
| CalDAV server unreachable | `503` with `Retry-After: 30`; the liveness endpoint turns unhealthy only after a sustained outage with active subscriptions |
| DAVbridge restarts | EWS clients get `ErrorSubscriptionNotFound` (answered with a short delay to avoid hot loops); open streams are closed cleanly on `SIGTERM` (`stop_grace_period: 20s`) |
| SQLite lost | Clients rebuild their view; no content lost |
| Unsupported or unmappable event | Not shown or read-only, never approximated ([limitations.md](limitations.md)) |
