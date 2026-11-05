# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, incompatible
changes to the configuration, the CLI or the persisted state may be released in a minor version;
patch releases do not change documented configuration or protocol behavior.

## [Unreleased]

### Fixed

- `tools/eas_test_client.py` scenario T5 expected `Settings` / `Set Oof` with `OofState` 0 to be rejected, although the documented behavior is
  "off is accepted without effect, on is rejected (status 7)". The scenario now checks both.

### Documentation

- README: new section "Before you try it" (use a separate test calendar and a backup, main limitations, Android untested, request for feedback);
  the Quickstart now checks for the external Docker network `proxy` before the first Compose command.
- README and client guides: "free/busy" now says what is meant (the busy status of a single event); free/busy lookups and the scheduling
  assistant remain unsupported.
- Revoking an app password: the mobile guide now states the same upper bound as the privacy guide (`auth.positive_cache_seconds`).
- Limitations: note that DAVbridge is not a Nextcloud app and that App Store license rules would have to be clarified first.

## [0.1.0] - 2026-11-05

First public release (alpha). DAVbridge is extracted from a single-operator deployment and
generalized for public use. State and identifiers of that deployment are intentionally **not**
compatible; only the configuration can be converted (`davbridge config migrate`).

### Added

- Calendar facade for **Outlook for Mac** over Exchange Web Services (EWS): folder and item
  operations, `SyncFolderItems`, pull and streaming notifications, user configuration objects and
  POX Autodiscover. See [docs/protocol-coverage.md](docs/protocol-coverage.md).
- Calendar facade for **Outlook for iOS and Android** over Exchange ActiveSync **14.1** only, including
  Provision, FolderSync, Sync (Add/Change/Delete/Fetch), Ping, ItemOperations (Fetch), Settings and
  mobile Autodiscover (POX `mobilesync` and JSON v2). Disabled by default; requires a double opt-in and an explicit acknowledgement
  that Microsoft's cloud processes a copy of the calendar
  (`protocols.eas.acknowledge_mobile_cloud_processing`).
- CalDAV backend (RFC 4791, RFC 6578 `sync-collection`) tested against Nextcloud / sabre/dav:
  conditional writes with ETags, lossless iCalendar patching.
- Optional **calendar autodiscovery** (`calendar_mode = "auto"`, off by default): after a successful login the principal and
  the calendar home are resolved and one `Depth: 1` `PROPFIND` lists the user's own readable `VEVENT` calendars (shared calendars
  only with `include_shared`); write access comes only from the server's privilege set and the operator rules; redirects, foreign
  origins and paths outside the calendar home are refused; metadata is cached for five minutes. The default calendar is never guessed
  (`dynamic_users.default_calendar`); an ambiguous or empty result is answered with HTTP 403.
- Optional **pattern admission** (`auth.mode = "pattern"`, high risk, off by default) with fully anchored patterns, plain-user-name
  validation and no CalDAV request for names outside the pattern.
- `GetUserOofSettings` (EWS) reports a disabled out-of-office state for the own mailbox.
- TOML configuration (`config_version = 1`) with strict validation, environment overrides
  (`DAVBRIDGE_*`, `DAVBRIDGE__SECTION__KEY`) and a converter for configuration files of the
  originating deployment (`davbridge config migrate`).
- `davbridge` CLI: `init`, `serve`, `healthcheck`, `check` (configuration, state directory, CalDAV server, optional sign-in and
  calendar test, `--public` probes, `--json`; exit 0/1/2), `backup` (SQLite online backup, optional `.tar` with key, config and checksums),
  `migrate --check`, `rotate-key`, `devices`, `reset`, `unblock`, `diagnose`, `config validate`, `config migrate`, `version`.
- `server.listen` is effective: `davbridge serve` binds the single uvicorn worker to it and the container health check follows it.
- Hardened container image (`ghcr.io/chr0mcom/davbridge`, linux/amd64 and linux/arm64): multi-stage build, non-root
  UID/GID 65532, read-only root file system compatible with all state in one volume, hash-pinned dependencies and build tools (`requirements/runtime.txt`, `build.txt`, `dev.txt`, wheel built with `--no-build-isolation`),
  OCI labels, built-in health check, no pip in the final image, SBOM and provenance attestation.
- Documentation, example Docker Compose file and example reverse proxy configurations for Traefik,
  Caddy and nginx.
- Security and supply-chain automation: CI, Gitleaks, Dependabot, release workflow (the image is published under the staging tag `sha-<full commit SHA>` first, which is never overwritten, and
  promoted to the version tags only after the smoke test and the vulnerability scan of both platform variants; the SPDX SBOMs are attached to the GitHub release); a weekly
  and manually startable integration workflow runs the integration tests and the EAS test client against a throw-away Nextcloud. CI runs the publication
  hygiene tests when the maintainer-only term list is available as a repository secret.
- `davbridge rotate-key --force`; the service removes `status.json` on a clean shutdown, and `rotate-key` prepares the new key and the backup of the old one
  before it discards any state.
- `davbridge backup` creates a missing target directory (one level, mode `0700`) and works on file systems without hard links.
- In pattern mode every spelling of a login that differs only in letter case maps to the user id the CalDAV server uses
  ([docs/configuration.md](docs/configuration.md)).

### Security

- The SQLite state database and its `-wal`/`-shm` files are created with mode `0600` (tightened at start if an existing file is
  more permissive). The database holds the raw client IP addresses of the lock-out table; [docs/privacy.md](docs/privacy.md) says how long and how to clear them
  (`davbridge unblock`).
- `hmac.key` is only accepted as a regular file (no symbolic link) owned by the running user without group or other permission bits; `serve` refuses
  to start otherwise and `check` reports an error.
- Calendar paths with an encoded `/`, `\` or `.` (for example `%2F`) are rejected in the configuration, in migrated files and during discovery.
- `server.trusted_proxies` rejects `/0` networks.
- Large CalDAV answers (above 256 KiB) are parsed in a worker thread so that they cannot stall the single event loop.
- Runtime dependencies are locked at versions without known fixable HIGH or CRITICAL vulnerabilities at release time:
  lxml 6.1 (CVE-2026-41066), Starlette 1.10 (CVE-2025-62727, CVE-2026-48818, CVE-2026-54283), FastAPI 0.162.

### Notes for operators

- The state database has schema version 3 and the application id `0x44415642`. An **older** schema is discarded and recreated at the next start (no in-place
  migration); a **newer** schema (a rollback) or a foreign SQLite file at the same path makes `davbridge serve` exit with code 2 and deletes nothing.
  `davbridge migrate --check` reports each case beforehand.
- `davbridge rotate-key` invalidates all identifiers and clears the state bound to the key; clients resynchronize and mobile
  devices may have to be re-provisioned ([docs/operations.md](docs/operations.md#key-rotation)).

### Known limitations

See [docs/limitations.md](docs/limitations.md). Highlights: calendar only (no mail, contacts or tasks);
no invitations, attendees or attachments; recurring events are not editable (and not shown at all in
Outlook for Mac); changes made on the CalDAV side may appear in Outlook for Mac only after a manual
"Synchronize" or a restart; EAS 14.1 only; no Modern Authentication.

[Unreleased]: https://github.com/chr0mcom/davbridge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/chr0mcom/davbridge/releases/tag/v0.1.0
