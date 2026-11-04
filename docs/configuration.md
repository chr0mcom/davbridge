# Configuration

DAVbridge is configured with one TOML file (default `/etc/davbridge/davbridge.toml`) plus optional
environment variables. A fully commented example is in
[`config/davbridge.example.toml`](../config/davbridge.example.toml).

- `config_version = 1` is mandatory.
- **Strict validation:** unknown keys, wrong types, duplicate users, ambiguous calendars, plain-HTTP URLs
  (outside explicit development modes) and inconsistent EAS switches prevent startup.
- **No CalDAV passwords** are accepted in the file or in the environment. Each client sends its own CalDAV (app)
  password; DAVbridge verifies it against the CalDAV server and keeps it in memory only.
- Check a configuration without starting the service: `davbridge config validate` (strict validation only) or
  `davbridge check` (validation plus state directory, CalDAV server and optional login test). Both accept
  `--show-effective-config` to print the merged configuration (file plus environment overrides); it contains no secrets.

Unless a default is stated, the built-in default equals the value in the example file; the secure defaults that
matter most are: authentication mode `allowlist`, no trusted proxies, EAS disabled, explicit calendars (no autodiscovery).

## Contents

1. [Schema](#schema)
2. [Environment variables and precedence](#environment-variables-and-precedence)
3. [User admission: allowlist vs pattern](#user-admission-allowlist-vs-pattern)
4. [Calendars: explicit vs auto](#calendars-explicit-vs-auto)
5. [Calendar discovery](#calendar-discovery)
6. [Outlook mobile (EAS): double opt-in](#outlook-mobile-eas-double-opt-in)
7. [Converting a legacy configuration](#converting-a-legacy-configuration)

## Schema

### Minimal secure example

```toml
config_version = 1

[server]
public_url = "https://dav.example.com"
listen = "0.0.0.0:8000"
default_timezone = "UTC"
autodiscover_culture = "en:us"
trusted_proxies = ["172.18.0.2/32"]

[caldav]
base_url = "https://cloud.example.com/remote.php/dav"
verify_tls = true
follow_redirects = false
connect_timeout_seconds = 5
read_timeout_seconds = 30

[auth]
mode = "allowlist"
allowed_users = ["alice"]
username_pattern = ""
address_template = "{user}@dav.example.com"
positive_cache_seconds = 300
failure_limit = 10
initial_lock_seconds = 900

[protocols.ews]
enabled = true
stream_max_seconds = 1800
event_detail = "both"

[protocols.eas]
enabled = false
acknowledge_mobile_cloud_processing = false
versions = ["14.1"]
window_max = 100
heartbeat_min_seconds = 60
heartbeat_max_seconds = 540
device_limit = 6
delete_brake_minimum = 5

[features]
event_fields = ["subject", "location", "time", "all_day", "body", "busy_status", "reminder", "categories", "sensitivity"]
read_only_owners = []
protected_property_prefixes = []
protected_event_writable_fields = ["subject", "location", "start", "end", "all_day", "timezone"]

[[users]]
username = "alice"
address = "alice@dav.example.com"
display_name = "Alice Example"
eas_enabled = false
calendar_mode = "explicit"

  [[users.calendars]]
  path = "primary"
  id = "primary"
  display_name = "Primary Calendar"
  default = true
  write = "caldav"
  owner = "alice"
```

### `[server]`

| Key | Type | Default | Description |
|---|---|---|---|
| `public_url` | string (HTTPS URL) | required | Public base URL exactly as clients reach it. Used in Autodiscover answers; never taken from the `Host` header. |
| `listen` | string `host:port` | `0.0.0.0:8000` | Address `davbridge serve` binds to (the container starts exactly this command); IPv6 hosts go in brackets, for example `[::]:8000`. `davbridge healthcheck` and the container health check probe the port from this value. Keep `8000` unless you also adapt your reverse proxy; direct access to this port must only be possible from the proxy network. |
| `default_timezone` | IANA zone name | `UTC` | Zone used for events without time zone information. |
| `autodiscover_culture` | string | `en:us` | Culture reported in the mobile Autodiscover response. |
| `trusted_proxies` | list of CIDRs | `[]` | Only these peers may set `X-Forwarded-For` / `X-Forwarded-Proto`. Empty = trust nobody; then every client appears with the proxy's address and shares one lock-out counter. Set it to your proxy, as narrowly as possible. |

### `[caldav]`

| Key | Type | Default | Description |
|---|---|---|---|
| `base_url` | string (HTTPS URL) | required | CalDAV base URL. For Nextcloud: `https://<host>/remote.php/dav`. Calendar collections are addressed as `<base_url>/calendars/<username>/<path>/`. |
| `verify_tls` | bool | `true` | Verify the CalDAV server certificate. Disable only for local experiments. |
| `follow_redirects` | bool | `false` | Follow HTTP redirects from the CalDAV server. Auto-discovery rejects redirects regardless of this setting. |
| `connect_timeout_seconds` | int | `5` | TCP/TLS connect timeout. |
| `read_timeout_seconds` | int | `30` | Per-request read timeout. |

### `[auth]`

| Key | Type | Default | Description |
|---|---|---|---|
| `mode` | `"allowlist"` \| `"pattern"` | `"allowlist"` | See [User admission](#user-admission-allowlist-vs-pattern). |
| `allowed_users` | list of strings | `[]` | Explicit user names (CalDAV logins) admitted in allowlist mode, in addition to the `[[users]]` entries. At least one user must exist overall. |
| `username_pattern` | string (regex) | `""` | Pattern mode only. Fully anchored (`fullmatch`). |
| `address_template` | string | `"{user}@<host of public_url>"` | Account address for users without an explicit `address`. `{user}` is the only allowed placeholder. |
| `positive_cache_seconds` | int | `300` | How long a verified password is trusted without asking the CalDAV server again. A revoked app password stops working after at most this long. |
| `failure_limit` | int | `10` | Failed logins per client IP before a lock-out starts. |
| `initial_lock_seconds` | int | `900` | First lock-out duration; repeated lock-outs double it (up to 24 h). A success resets the counter. Anonymous requests without credentials do not count. |

Other limits (each EWS and EAS lock-out list is separate, up to four app passwords per user are valid at the same time,
unknown passwords for a user are throttled to one CalDAV check per minute once a user has many failures) are fixed.

### `[protocols.ews]`

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Serve Outlook for Mac (EWS and POX Autodiscover). |
| `stream_max_seconds` | int | `1800` | Maximum length of one streaming-notification connection. Valid range 60-1800. Your proxy read timeout must be at least this plus 100 s. |
| `event_detail` | `"folder"` \| `"item"` \| `"both"` | `"both"` | Granularity of notification events. Keep `both` unless instructed otherwise. |

### `[protocols.eas]`

Disabled by default. See [EAS double opt-in](#outlook-mobile-eas-double-opt-in) and [privacy.md](privacy.md).

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Global switch. |
| `acknowledge_mobile_cloud_processing` | bool | `false` | Must be `true` to start with EAS enabled: you confirm that you read [privacy.md](privacy.md) and accept that Microsoft's cloud processes a copy of the calendar. |
| `versions` | list of strings | `["14.1"]` | Only `14.1` is supported. |
| `window_max` | int | `100` | Maximum items per Sync response window (range 1-512). |
| `heartbeat_min_seconds` | int | `60` | Smallest accepted Ping/Sync-wait interval. |
| `heartbeat_max_seconds` | int | `540` | Largest accepted interval (`60 <= min <= max <= 540`). Proxy read timeout must be >= 650 s. |
| `device_limit` | int | `6` | Devices per user (range 1-20). When exceeded, the least recently used device is replaced; nobody is locked out. |
| `delete_brake_minimum` | int | `5` | Delete brake: at most `max(delete_brake_minimum, 10 % of known items)` client deletions per user and hour (range 1-1000). |
| `wait_max_minutes` | int | `9` | Largest accepted `Wait` of a Sync request, in minutes (range 1-9); a larger value is answered with status 14 and this limit. |
| `ping_limit_per_user` | int | `4` | Concurrently waiting `Ping` requests per user (range 1-8); further ones are answered with `503`. |
| `device_expiry_days` | int | `30` | Device records and sync states unused for this long are removed (range 1-365). |
| `request_max_seconds` | int | `25` | Time budget of one non-waiting EAS request (range 5-60); on expiry the answer is `503` with `Retry-After: 30`. |
| `request_body_max_bytes` | int | `524288` | Largest accepted EAS request body (range 1024-1048576). |
| `response_max_bytes` | int | `786432` | Upper bound for one EAS response; larger result sets are paged (range 65536-4194304). |
| `body_max_chars` | int | `32768` | Event descriptions longer than this are truncated for EAS (range 1024-262144). |

Out-of-range values, a non-integer value or a heartbeat range that violates `60 <= min <= max <= 540` prevent startup; `davbridge config validate`
reports them. The EAS keys without a short environment variable can be overridden with `DAVBRIDGE__PROTOCOLS__EAS__<KEY>`.

### `[features]`

| Key | Type | Default | Description |
|---|---|---|---|
| `event_fields` | list of strings | all nine shown in the example | Event fields that may be read and written. Only known, tested fields can be listed: `subject`, `location`, `time`, `all_day`, `body`, `busy_status`, `reminder`, `categories`, `sensitivity`. In this version exactly two sets are accepted: all nine fields (the default), or all except `categories` and `sensitivity`. Anything else prevents startup. |
| `read_only_owners` | list of strings | `[]` | CalDAV users whose calendars are always read-only through DAVbridge. An additional operator rule; nothing is built in. |
| `protected_property_prefixes` | list of strings | `[]` | Complete, upper-case iCalendar property-name prefixes such as `X-EXAMPLE-`. An event carrying at least one such property can be changed through EAS only in the fields of `protected_event_writable_fields`. Empty = no special handling. |
| `protected_event_writable_fields` | list of strings | `["subject","location","start","end","all_day","timezone"]` | Fields that EAS may still change on such protected events. Known names: `subject`, `location`, `start`, `end`, `all_day`, `timezone`, `body`, `busy_status`, `reminder`, `categories`, `sensitivity`. |

The feature set is closed: mail, contacts, tasks, attendees, invitations, attachments, writing recurring events, OAuth
and EAS 16.x **cannot** be enabled through configuration (see [limitations.md](limitations.md)).

### `[[users]]`

One block per explicitly configured user.

| Key | Type | Default | Description |
|---|---|---|---|
| `username` | string | required | CalDAV login name. Must be unique. |
| `address` | string | `address_template` | E-mail-style account address that the user types into Outlook. It does not need to receive mail, but its domain should resolve to DAVbridge so Autodiscover works. |
| `display_name` | string | `username` | Name shown in Outlook. |
| `eas_enabled` | bool | `false` | Per-user opt-in for Outlook mobile. Requires `[protocols.eas] enabled = true` and the acknowledgement. |
| `calendar_mode` | `"explicit"` \| `"auto"` | `"explicit"` | See [Calendars](#calendars-explicit-vs-auto). With `"auto"` the user has no `[[users.calendars]]` entries (listing some is an error); the calendars are discovered after login using the rules of `[dynamic_users]`. |

### `[[users.calendars]]`

Required for `calendar_mode = "explicit"`, not allowed with `"auto"`. Exactly one calendar per user must have `default = true`; identifiers and paths must be unique per user.

| Key | Type | Description |
|---|---|---|
| `path` | string | Name of the calendar collection below the user's calendar home. Nextcloud names its default calendar `personal`; other calendars use a slug derived from their name. Allowed characters: letters, digits and `._@%+-`. |
| `id` | string | Stable identifier that clients see (`[a-z0-9-]`, up to 40 characters; reserved folder names such as `calendar`, `inbox` or `tasks` are not allowed). Choose once and never change it - a change makes clients rebuild the calendar. |
| `display_name` | string | Calendar name shown in Outlook. Display names are never used as identities. |
| `default` | bool | Marks the default calendar (Outlook's "Calendar" folder). |
| `write` | `"caldav"` \| `"never"` | `"caldav"`: writing is allowed if configured here **and** the CalDAV server grants the privilege. `"never"` always wins. |
| `owner` | string | CalDAV user name of the calendar's owner (used for `read_only_owners`). |

### `[dynamic_users]`

Rules for users that have no explicit calendar list: users in `auth.mode = "pattern"`, entries of `auth.allowed_users` without a
`[[users]]` block, and `[[users]]` blocks with `calendar_mode = "auto"`. See [below](#calendar-discovery).

| Key | Type | Default | Description |
|---|---|---|---|
| `calendar_mode` | `"explicit"` \| `"auto"` | `"explicit"` | `"auto"` is the opt-in that allows calendar discovery for users without a `[[users]]` block. It is **required** for `auth.mode = "pattern"` and for `allowed_users` entries without a `[[users]]` block; with `"explicit"` those configurations prevent startup. |
| `default_calendar` | `"only"` \| `"first-writable"` \| exact path or id | `"only"` | How the default calendar is chosen. `"only"` accepts exactly one visible collection. With several, name the exact calendar path (the last path segment, for example `personal`) or its derived id (`c-` plus 24 hex digits, shown by `davbridge check`). `"first-writable"` (alphabetically first calendar that is writable and not owned by a `read_only_owners` user) is an explicit opt-in. |
| `include_shared` | bool | `false` | Include calendars shared with the user (they keep the rights the server grants; a share of a `read_only_owners` user is always read-only). |
| `eas_enabled` | bool | `false` | Outlook mobile for these users also needs this second opt-in. |

## Environment variables and precedence

The TOML file is the canonical configuration. Environment variables override it.

| Variable | Target |
|---|---|
| `DAVBRIDGE_CONFIG` | Path of the TOML file (default `/etc/davbridge/davbridge.toml`) |
| `DAVBRIDGE_PUBLIC_URL` | `server.public_url` |
| `DAVBRIDGE_CALDAV_URL` | `caldav.base_url` |
| `DAVBRIDGE_STATE_DIR` | State directory (default `/var/lib/davbridge`) |
| `DAVBRIDGE_LOG_LEVEL` | Log level of the structured log: `debug`, `info` (default), `warning` or `error` |
| `DAVBRIDGE_LOGS` | Directory for optional daily JSON-lines log files (`davbridge-YYYY-MM-DD.jsonl`, 30 days retention). Unset (default) = stdout only. Must be writable by the service user; it is not part of the state you need to back up. |
| `DAVBRIDGE_ALLOW_INSECURE_HTTP` | Strictly `true` or `false` (default `false`). `true` accepts plain `http://` URLs for `server.public_url` and `caldav.base_url`. **Development and CI only** (a throw-away Nextcloud on localhost); `davbridge check` warns while it is set. |
| `DAVBRIDGE_TRUSTED_PROXIES` | JSON list of CIDRs, e.g. `["172.18.0.2/32"]` |
| `DAVBRIDGE_ALLOWED_USERS` | JSON list of explicit user names |
| `DAVBRIDGE_USER_PATTERN` | Fully anchored pattern for dynamic admission |
| `DAVBRIDGE_ADDRESS_TEMPLATE` | Address template with `{user}` only |
| `DAVBRIDGE_CALENDAR_MODE` | `dynamic_users.calendar_mode`: `explicit` (default) or `auto` |
| `DAVBRIDGE_EAS_ENABLED` | Strictly `true` or `false` |
| `DAVBRIDGE_ACKNOWLEDGE_MOBILE_CLOUD_PROCESSING` | Strictly `true` or `false` |

In addition, **any scalar key** can be overridden with `DAVBRIDGE__SECTION__KEY` (double underscores separate the
levels), for example:

```bash
DAVBRIDGE__CALDAV__READ_TIMEOUT_SECONDS=60
DAVBRIDGE__PROTOCOLS__EWS__STREAM_MAX_SECONDS=900
```

Lists are **JSON**, not comma-separated. Unknown variables starting with `DAVBRIDGE__` are errors.

**Precedence (lowest to highest):** built-in secure defaults -> TOML file -> general `DAVBRIDGE__SECTION__KEY`
variables -> the named short variables above.

`DAVBRIDGE_CONFIG`, `DAVBRIDGE_STATE_DIR`, `DAVBRIDGE_LOGS`, `DAVBRIDGE_LOG_LEVEL` and `DAVBRIDGE_ALLOW_INSECURE_HTTP` are read from the environment only; they have no TOML key.
`GIT_COMMIT` (set by the image build) is shown by `davbridge version`.

Print the effective, merged configuration (without secrets) with `davbridge config validate --show-effective-config` or `davbridge check --show-effective-config`.

## User admission: allowlist vs pattern

A user can use DAVbridge only if (1) the normalized Basic-auth user name is admitted and (2) the credentials are
valid on the CalDAV server. Unknown users never reach the CalDAV server.

### `mode = "allowlist"` (default, recommended)

The user name must equal an entry of `auth.allowed_users` or a `[[users]]` block exactly (the address of a `[[users]]` block works as login name too). At least one user is required.
An `allowed_users` entry without a `[[users]]` block gets its calendars by [discovery](#calendar-discovery); that requires `[dynamic_users] calendar_mode = "auto"`
and an address derived from `address_template`.

### `mode = "pattern"` (high risk)

```toml
[auth]
mode = "pattern"
username_pattern = "^[a-z][a-z0-9._-]{0,63}$"
address_template = "{user}@dav.example.com"

[dynamic_users]
calendar_mode = "auto"
default_calendar = "only"
include_shared = false
eas_enabled = false
```

> **Warning.** In pattern mode **every user whose name matches the pattern and who has a valid CalDAV password can use
> the bridge**, i.e. everybody who can log in to your CalDAV server. On an internet-facing server this exposes the
> EWS/EAS endpoints to all those accounts and makes DAVbridge a credential-checking oracle for them. Use it only when
> that is exactly what you want (for example a small private deployment), keep the pattern as narrow as possible, and
> expect `davbridge check` and `davbridge config validate` to print a warning.

Rules:

- The pattern is matched with `fullmatch`, not as a substring search, and must start with `^` and end with `$`.
- The matched name must additionally be a plain user name (letters, digits and `._@+-`, at most 64 characters, starting with a letter or digit): nothing that
  could inject a URL, slash, percent-encoding or host name into CalDAV paths is ever admitted, whatever the pattern says.
- The login can also be the address of the user (`address_template` with the user name filled in); the extracted name must match the pattern too.
- At the first sign-in of a name DAVbridge asks the CalDAV server for the user id it uses (the user id in the `current-user-principal` of the signed-in user). If that id differs from the typed name only in letter case, the server's spelling is the account name from then on: `Alice`, `alice` and `ALICE@host` are one account with one set of sync states, calendar ids and notification loops, and requests to the server use the server's spelling. Any other answer keeps the name exactly as typed. The pattern is matched against what was typed, so a pattern that allows only lower-case letters also refuses `Alice` before any request is made.
- `[[users]]` blocks and `allowed_users` entries take precedence over the pattern and are not renamed: their name is the configured one (matched case-insensitively against the login).
- Names outside the pattern are refused **without any request to the CalDAV server**.
- `auth.mode = "pattern"` requires `[dynamic_users] calendar_mode = "auto"`: calendars cannot be configured per dynamic user.
- EAS stays off for dynamic users unless `[dynamic_users] eas_enabled = true` **and** the global opt-in are set.

## Calendars: explicit vs auto

| | `explicit` (default) | `auto` |
|---|---|---|
| Calendars | Exactly those listed in `[[users.calendars]]` | Discovered after login |
| Exploration of the CalDAV server | None | Bounded discovery (below) |
| Default calendar | The one with `default = true` | `default_calendar` rule |
| Calendar changes | Edit the file | Appear automatically (within five minutes); a change in the set can cause a full rebuild in clients |
| Risk | Lowest | Wrong default calendar or unexpected calendar if the rules are loose |

`explicit` is the safe default. `auto` is a deliberately chosen, narrowly bounded alternative, not free WebDAV exploration.

## Calendar discovery

After a successful login in `auto` mode:

1. `PROPFIND` on the authenticated principal to get `current-user-principal` and `calendar-home-set`.
2. Exactly one `PROPFIND` with `Depth: 1` on that calendar home.
3. Only collections with resource type `{DAV:}collection` + `{urn:ietf:params:xml:ns:caldav}calendar`, read access and a
   `supported-calendar-component-set` containing `VEVENT` are used.
4. Shared calendars are excluded by default (`include_shared = false`); the owner principal must be the signed-in principal.
5. Write permission comes **only** from `current-user-privilege-set`, a server-side read-only hint and the operator rules
   (`write`, `read_only_owners`). Missing or contradictory information results in read-only.
6. Any redirect, other scheme, host or port, and any path outside the canonical calendar home are rejected; hrefs are
   normalized, decoded once and checked for traversal.
7. Metadata (not content) is cached for five minutes.

The public calendar ID is `c-` followed by 24 hex digits derived with HMAC (from `hmac.key`, the user and the canonical collection path). Discovered
calendars have no explicit `id` setting. **Renaming or moving a calendar on the server, or rotating `hmac.key`, produces a new ID and a full resync.**
Display names are never identities.

The default calendar is never guessed. `default_calendar = "only"` accepts exactly one visible collection. A typical Nextcloud user owns a
personal calendar and a read-only birthday calendar (`contact_birthdays`), so `"only"` fails there: set `default_calendar = "personal"` (or the path of the
calendar you want). If the default cannot be determined, or the user has no usable calendar at all, the login is refused with HTTP `403` (EWS and EAS),
no `WWW-Authenticate` challenge, and a `login_rejected` log record with `reason = "no-default-calendar"`. The result of a failed discovery is remembered for one minute so a client
retry loop does not hammer the CalDAV server.
`davbridge check --user alice --password-stdin` lists the candidate calendars (names, paths, rights; no event content) and reports which one would become the default.

The discovery assumes the Nextcloud / sabre/dav layout: the calendar home must be exactly `<base_url>/calendars/<user>/` and the principal
`<base_url>/principals/users/<user>/`; anything else is refused.

## Outlook mobile (EAS): double opt-in

Outlook for iOS/Android connects through Microsoft-operated cloud services; a copy of the calendar,
**including private events and descriptions**, is processed by Microsoft. DAVbridge therefore refuses to enable EAS
without **three** explicit settings:

```toml
[protocols.eas]
enabled = true                               # 1. global switch
acknowledge_mobile_cloud_processing = true   # 2. you read docs/privacy.md and accept the cloud processing

[[users]]
username = "alice"
eas_enabled = true                           # 3. per-user opt-in
```

Startup fails if only some of them are set (for example `eas_enabled = true` for a user while EAS is off, or EAS on with no
user enabled, or EAS on without the acknowledgement). The environment equivalents are
`DAVBRIDGE_EAS_ENABLED` and `DAVBRIDGE_ACKNOWLEDGE_MOBILE_CLOUD_PROCESSING`.

Enabling or disabling EAS requires recreating the container; it closes the open EWS streams, so Outlook for Mac users
may have to use "Synchronize" afterwards.

## Converting a legacy configuration

`davbridge config migrate` converts a configuration file of the originating single-operator deployment
(`bruecke.toml`, German key names) to the current format. It works offline.

```bash
docker compose run --rm -v "$PWD:/work" davbridge davbridge config migrate \
    --from /work/bruecke.toml --to /work/davbridge.toml
```

- It **never overwrites** an existing target file, never follows symbolic links, does not read password files, writes
  atomically with mode `0600`, then loads the result with the production validator and prints an English migration report.
- It preserves your concrete owners, users, domains and calendars; none of it becomes a built-in default.
- Obsolete or unsafe switches (`messmodus_tolerant`, `delete_ohne_changekey`) are not carried over and are reported.
- A legacy `stufe` of `M2` maps to the default `features.event_fields`. `M0`, `M1` and `M3` abort with an explanation unless you
  pass `--feature-profile`.
- `eas.aktiv = true` becomes `protocols.eas.enabled = true`, but the converter **never** sets
  `acknowledge_mobile_cloud_processing`. Read [privacy.md](privacy.md) and add it yourself.
- Only the configuration is converted. The old SQLite state and HMAC key are **not** converted; clients rebuild their
  view once (new identifier space).

Key mapping:

| Legacy | Current |
|---|---|
| `basis_url` | `server.public_url` |
| `caldav_basis` | `caldav.base_url` |
| `stream_max_s` | `protocols.ews.stream_max_seconds` |
| `ereignis_detail = ordner\|element\|beide` | `event_detail = folder\|item\|both` |
| `zugang_pruef_s`, `sperre_*` | the corresponding keys in `[auth]` |
| `[richtlinie].nur_lesen_eigentuemer` | `[features].read_only_owners` |
| `[[konto]]` `name`/`adresse`/`anzeigename`/`eas` | `[[users]]` `username`/`address`/`display_name`/`eas_enabled` |
| `[[konto.kalender]]` `uri`/`schluessel`/`anzeigename`/`standard`/`eigentuemer` | `[[users.calendars]]` `path`/`id`/`display_name`/`default`/`owner` |
| `schreiben = nextcloud\|nie` | `write = caldav\|never` |
| `[eas]` limits | `[protocols.eas]` with English names |
| `stufe = M2` | default `features.event_fields` |
| `stufe = M0\|M1\|M3` | abort unless `--feature-profile` |
| `messmodus_tolerant`, `delete_ohne_changekey` | not carried over; reported as obsolete/unsafe |
