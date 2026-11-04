# Privacy

> **Outlook for iOS and Android connects through Microsoft-operated cloud services.** If you enable
> Outlook mobile (EAS), **all calendar data, including private events and their descriptions, can be copied to and
> processed by Microsoft**, and Microsoft's service needs the account credentials (your app password) to reach DAVbridge.
> DAVbridge does not control the region, retention or deletion of that copy. Read this page completely before
> enabling it. EAS is disabled by default and can only be switched on with an explicit acknowledgement.

DAVbridge is independent, community-developed software. It is not affiliated with, endorsed by or certified by Microsoft or
Nextcloud. This page describes what the **software** does with data. The project operates no service, no telemetry and no
hosting; whoever runs DAVbridge is the operator and controls the data.

## Data flows

| Flow | Data | Who sees it |
|---|---|---|
| Outlook for Mac -> reverse proxy -> DAVbridge | Credentials (HTTP Basic over TLS), calendar requests and responses | You (operator); your proxy sees credentials in transit |
| DAVbridge -> CalDAV server | The user's own CalDAV credentials (as sent by the client), calendar requests, event data | Your CalDAV server |
| Outlook mobile app <-> Microsoft cloud <-> DAVbridge | Credentials and **a synchronized copy of the calendar** | Microsoft (per its terms) and you |
| DAVbridge -> anywhere else | Nothing. No telemetry, no update check, no external API. | - |

## What DAVbridge stores and logs

| Place | Content | Notes |
|---|---|---|
| Memory | Verified CalDAV passwords (hashed cache keys and the credential needed to call CalDAV on behalf of a user), EWS subscriptions, EAS ping state | Lost on restart. Passwords are **never** written to disk or to the configuration. |
| `davbridge.sqlite` (state volume, mode `0600`) | Sync states and sync tokens, device records (hashed device ID, device type, model/OS strings that match a strict pattern), lock-out counters **with the raw client IP addresses**, de-duplication data, user configuration objects of EWS clients | **No event content** (no titles, locations, attendees, descriptions). See [Client IP addresses and lock-outs](#client-ip-addresses-and-lock-outs). |
| `hmac.key` | Secret key | Anyone holding it can derive identifiers; protect it like a password. |
| Logs (stdout; optional files) | Event kind, operation or command name, user name, response codes, counts, durations, request ID, hashed object and device identifiers | Built from an allow-list with strict patterns. **Never** passwords, event content, raw device IDs, `ClientId`, raw request bodies or `Authorization` headers. Logs contain no client addresses. Protocol-capture files (structure only: element names and enumeration values, no text values) can be written for accounts that are flagged as test accounts in code; no configuration option turns this on in v0.1, so a normal installation never writes them. |

Things DAVbridge cannot control: your reverse proxy's access logs (URL paths, IP addresses, user agents), your CalDAV server's
logs, Docker log retention, backups you make, and everything Microsoft stores. Configure those yourself.

## Client IP addresses and lock-outs

To slow down password guessing, DAVbridge counts failed logins per client address. These counters live in the table `lockout` of `davbridge.sqlite` and contain the
**raw IP address** of every client that failed a login (an EWS and an EAS entry, the latter prefixed `eas:`), the number of recent failures, the lock-out level and timestamps.
Successful logins reset the counters but do not delete the row immediately.

- **How long:** a background job runs every minute. Rows that are not blocked are deleted 15 minutes after the last failure (rows that have already led to a lock-out
  are kept for up to 24 hours after their last activity so that repeated lock-outs get longer), and at most 5000 rows are kept. A lock-out lasts `auth.initial_lock_seconds`
  (default 900 s), doubling with each repetition up to 24 hours.
- **Behind a reverse proxy** the address is the one the proxy reports via `X-Forwarded-For`, but only if the proxy is listed in `server.trusted_proxies`; otherwise it is the proxy's own address.
- **Clearing it:** `davbridge unblock --ip ADDRESS` removes one address (EWS and EAS entry), `davbridge unblock --all` removes every entry, and `davbridge reset all --yes-i-understand`
  empties the whole database. The state backups (`davbridge backup`) contain the table as well, so treat them accordingly. The database file is created with mode `0600`.
- The IP addresses are not written to the logs and are never sent anywhere. The same table also holds per-user throttle entries (`k:<user name>`) for EAS.

If you subject IP addresses to data-protection rules (they can be personal data), cover this storage in your own records of processing and retention decisions.

## Outlook mobile and the Microsoft cloud

Microsoft documents that Outlook for iOS and Android does not connect from the phone directly to non-Microsoft Exchange-style
servers that use Basic authentication; instead a Microsoft-operated service synchronizes with the server and the app
talks to that service. Consequences for your deployment:

1. **A copy of the calendar exists at Microsoft**: titles, times, locations, descriptions, categories, reminders, sensitivity
   flags - including events marked private and data of third parties contained in event text.
2. Microsoft's service must reach DAVbridge over the internet and must present the **credentials** you set up. Use a
   dedicated, revocable app password with minimal rights.
3. Region, retention and deletion are governed by Microsoft's terms and privacy documentation and by the account/app
   configuration, not by DAVbridge. Removing the account from the app or revoking the app password stops synchronization;
   **DAVbridge cannot delete data already held by Microsoft.**
4. The behavior of Microsoft's cloud can change without notice.

Microsoft's own description: *Using Basic authentication with Outlook for iOS and Android* (Microsoft Learn) and the
*Outlook for iOS and Android FAQ*.

## Operator duties

If you run DAVbridge you are responsible for, at least:

- deciding whether the Microsoft cloud processing is acceptable for **every user and every event** (employees, customers,
  family, third-party data); contracts, privacy notices, legal basis and international transfers are yours to assess;
- obtaining the consent or other basis you need from the people whose calendars are connected;
- enforcing TLS with a publicly trusted certificate, unique **app passwords** per device that you can revoke, and
  the default `allowlist` mode;
- keeping `eas_enabled` off for everyone who does not need Outlook mobile;
- securing and backing up the state volume and `hmac.key`; limiting proxy and Docker logs;
- licensing, patent and trademark questions for your use of Outlook and Microsoft protocols;
- keeping the software up to date.

## Revocation and removal

| Goal | Action |
|---|---|
| Stop one device immediately | Revoke its app password on the CalDAV server (effective at the latest after `auth.positive_cache_seconds`) |
| Stop Outlook mobile for a user | Set `eas_enabled = false` and restart; `davbridge reset devices --user USER`; remove the account in the app |
| Stop Outlook mobile for everyone | `protocols.eas.enabled = false`, restart |
| Remove stored client IP addresses | `davbridge unblock --all` (all lock-out rows), or wait for the automatic deletion described above |
| Remove local state | `davbridge reset all --yes-i-understand` (keeps `hmac.key` and configuration) |
| Remove everything | `docker compose down -v`, delete the configuration, revoke all app passwords |
| Remove the copy at Microsoft | Not possible from DAVbridge. Use Microsoft's account and privacy controls. |

## Public issues and bug reports

Issues, pull requests and discussions on GitHub are public. **Never post passwords, app passwords, `hmac.key`, tokens,
real calendar data or complete logs.** Redact user names and host names if you prefer. Report security problems privately
([SECURITY.md](../SECURITY.md)).
