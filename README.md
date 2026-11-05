# DAVbridge

**An independent calendar bridge that lets Outlook for Mac (Exchange Web Services) and Outlook for iOS/Android
(Exchange ActiveSync 14.1) use a CalDAV server - such as Nextcloud - as their calendar.**

> **Status: v0.1 - alpha.** Calendar only, limited feature set, no support commitment. Read [Limitations](#limitations) before you rely on it.

## Disclaimer

DAVbridge is an independent, community-developed calendar bridge. It is **not affiliated with, endorsed by, or certified by Microsoft or
Nextcloud**.

The bridge provides *limited* interoperability between supported Microsoft Outlook clients and a CalDAV calendar (tested with Nextcloud).
It does **not** implement a complete Microsoft Exchange server. Supported features, tested client versions and known limitations are
documented separately.

**Outlook for iOS and Android connects through Microsoft-operated cloud services.** Calendar data - including private events and
descriptions - and credentials may therefore be processed by Microsoft in accordance with Microsoft's terms and privacy
documentation. See [Privacy warning](#privacy-warning-outlook-for-ios-and-android) and [docs/privacy.md](docs/privacy.md).

The software is provided under the [MIT license](LICENSE) and **without warranty** to the extent permitted by applicable law. Operators are
responsible for assessing their own security, privacy, licensing, patent, trademark and backup requirements. The MIT license grants no
rights in Microsoft patents or trademarks, and this project makes no claim that patent licenses are unnecessary ([NOTICE](NOTICE)).

Microsoft, Outlook, Exchange, ActiveSync and Microsoft 365 are trademarks of the Microsoft group of companies. Nextcloud is a trademark of
Nextcloud GmbH. All other trademarks are the property of their respective owners. Product names are used only to describe technical
interoperability.

## What it is

DAVbridge is a small web service. To the Outlook client it looks like a calendar-only subset of an Exchange server; to your CalDAV server it
behaves like a normal CalDAV client of the signed-in user. The CalDAV server stays the single source of truth - DAVbridge stores no event content.

```text
 Outlook for Mac ---------- EWS + Autodiscover (HTTPS) ----------+
                                                                 v
                                                  +----------------------------+          +---------------------+
                                                  | reverse proxy (TLS)        |  HTTP    |     DAVbridge       |  CalDAV   +--------------+
                                                  | dav.example.com            |--------->| EWS / EAS facade    |---------->| Nextcloud /  |
                                                  +----------------------------+          | mapping, sync state |  (HTTPS)  | sabre/dav    |
                                                                 ^                        +---------------------+           | cloud.       |
 Outlook for iOS/Android <-- app --> Microsoft cloud -- EAS 14.1 (HTTPS) --+                                                 | example.com  |
                                       (copy of the calendar!)                                                              +--------------+
```

- **Mac:** the Mac talks to DAVbridge directly.
- **Mobile:** the phone talks to a Microsoft-operated service, and that service talks to DAVbridge. A copy of the calendar lives at Microsoft.

## Features

- Outlook for Mac over **EWS**: view, create, edit and delete single events; reminders, categories, all-day, the busy status of the single event (free/tentative/busy/out of office), private flag. Querying other people's availability (free/busy lookups, scheduling assistant) is **not** supported.
- Outlook for iOS/Android over **Exchange ActiveSync 14.1** (disabled by default, double opt-in): the same for single events; recurring events read-only where exactly mappable.
- Autodiscover (POX and JSON v2) for easy account setup.
- Conditional writes (ETags), lossless patching of existing events (unknown properties, extra alarms and time zones survive), CalDAV wins on conflicts.
- Allow-list authentication against your CalDAV server using app passwords; lock-outs, throttling, no passwords on disk. Optional (off by default, high risk): pattern admission and automatic calendar discovery.
- One small container: non-root (UID 65532), read-only root file system, `linux/amd64` and `linux/arm64`, SBOM and provenance.
- No telemetry. Logs contain no event content and no credentials.
- CLI: `davbridge init`, `check`, `serve`, `backup`, `migrate --check`, `rotate-key`, `reset`, `devices`, `unblock`, `diagnose`, `config validate`, `config migrate`, `version`
  ([operations](docs/operations.md)).

## Tested with

As of 2026-11-04. Other versions may work but are **untested**.

| Component | Tested version |
|---|---|
| CalDAV server | Nextcloud server 34.0.5 (CalDAV / sabre/dav) |
| Outlook for Mac | 16.114 (build 26102031, `MacOutlook/16.114.26102031`) via EWS |
| Outlook for iOS | App Store version current on 2026-11-04 (the Microsoft cloud service identifies as `Outlook-iOS-Android/1.0`) via Exchange ActiveSync 14.1 |
| Outlook for Android | **Not tested** |

Real Outlook binaries are tested manually; the automated suite uses synthetic protocol fixtures and a throw-away Nextcloud.

## Limitations

The short version (the complete, honest list is in [docs/limitations.md](docs/limitations.md)):

- **Calendar only.** No mail, contacts, tasks. Outlook mobile shows empty mail views; sending is rejected.
- **No invitations, attendee editing, meeting responses, attachments.**
- **Recurring events:** not shown at all in Outlook for Mac; read-only (if exactly mappable) in Outlook mobile. Outlook shows **no error** when
  you try to create them - do not.
- **Outlook for Mac does not pick up server-side changes by itself.** Use right-click -> *Synchronize* or restart Outlook.
  Subscriptions are lost when DAVbridge restarts.
- Conflicts: the CalDAV version wins. A delete brake limits mass deletions from mobile clients.
- EAS **14.1 only**; no Modern Authentication / OAuth; no general Microsoft 365 or Exchange compatibility; other CalDAV servers untested.
- Exactly one instance; public DNS and a publicly trusted TLS certificate are required.
- Microsoft can change client or cloud behavior at any time. No support, availability or compatibility guarantee.

## Privacy warning: Outlook for iOS and Android

> Outlook mobile does **not** connect your phone directly to DAVbridge. It connects through **Microsoft-operated cloud services**.
> **A synchronized copy of your calendar - including private events and descriptions - can be stored and processed by Microsoft**, and Microsoft's
> service needs your credentials (use a dedicated, revocable app password) to reach DAVbridge. You and DAVbridge do not control region, retention
> or deletion of that copy. Third-party data in events is affected too.
>
> Mobile support is **off by default**. It starts only if the operator sets `protocols.eas.enabled`, `acknowledge_mobile_cloud_processing`
> **and** `eas_enabled` for the user. Read [docs/privacy.md](docs/privacy.md) first. Outlook for Mac does not involve a Microsoft cloud service in this path.

## Before you try it

This is an **alpha** from a single-operator deployment. Please test it with a **separate test calendar** (and a test user or a dedicated app
password) and keep a **backup** of any calendar you connect. Do not point it at your only productive calendar yet.

- Recurring events are **not shown at all** in Outlook for Mac. Creating recurring events, attachments or invitations in Outlook can end in a
  **silent divergence**: Outlook keeps something locally that does not exist like that on the server. Do not create them.
- Outlook for Mac does not reliably pick up server-side changes by itself; use *Synchronize* or restart Outlook.
- Outlook for Android is **not tested**; Outlook for iOS and Mac were tested manually.
- The delete brake delays repeated mass deletions from mobile clients and may execute rejected deletions later ([docs/limitations.md](docs/limitations.md)).
- Feedback from other setups is very welcome: open an issue with your versions and what you tested ([CONTRIBUTING.md](CONTRIBUTING.md)).

## Quickstart (about 15 minutes)

Prerequisites: a Linux host with Docker and Compose; a DNS name (here `dav.example.com`) with a **publicly trusted TLS certificate** on a
reverse proxy; a CalDAV server reachable from the container (here Nextcloud at `cloud.example.com`); a CalDAV **app password** for your user
(required when 2FA is on). Details: [docs/installation.md](docs/installation.md).

The example Compose file attaches the container to an **existing external Docker network named `proxy`** (the network your reverse proxy is on),
and `docker compose run` fails with "network proxy declared as external, but could not be found" if it does not exist. Check first:

```bash
docker network ls --filter name=^proxy$ --format '{{.Name}}'   # prints "proxy" if it exists
docker network create proxy                                    # only if you have no proxy network yet
```

If your reverse proxy already uses another network, keep that one and change `networks.proxy.name` in `docker-compose.yml` after step 1.

```bash
# 1. Get the example files
mkdir -p ~/davbridge && cd ~/davbridge
curl -fsSLO https://raw.githubusercontent.com/chr0mcom/davbridge/v0.1.0/examples/compose/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/chr0mcom/davbridge/v0.1.0/config/davbridge.example.toml -o davbridge.toml

# 2. Edit davbridge.toml: public_url, caldav.base_url, trusted_proxies, your user and calendar path
$EDITOR davbridge.toml

# 3. One-time initialization (creates the secret hmac.key in the state volume)
docker compose run --rm davbridge davbridge init

# 4. Validate (configuration, state directory, CalDAV reachability, login, calendars) - nothing is changed or read from your events
docker compose run --rm davbridge davbridge check
read -rsp "App password: " PW; echo
printf '%s' "$PW" | docker compose run --rm -T davbridge davbridge check --user alice --password-stdin; unset PW

# 5. Start, then attach your reverse proxy (examples/reverse-proxy: Traefik, Caddy, nginx)
docker compose up -d
docker compose exec davbridge davbridge check --public
```

Then connect a client:

- **Outlook for Mac:** [docs/clients/outlook-mac.md](docs/clients/outlook-mac.md) - account type *Exchange*, user name `alice`, the app password.
- **Outlook for iOS/Android:** read the privacy warning above, then [docs/clients/outlook-mobile.md](docs/clients/outlook-mobile.md).

The reverse proxy needs specific settings (no buffering or compression, read timeouts of 1900 s / 650 s, no SSO in front, correct trusted proxies):
see [docs/reverse-proxy.md](docs/reverse-proxy.md).

## Documentation

| Document | Content |
|---|---|
| [docs/installation.md](docs/installation.md) | Requirements, Compose, `init`, permissions, 30-minute acceptance |
| [docs/configuration.md](docs/configuration.md) | Full schema, environment variables, allowlist vs pattern, calendars, EAS opt-in, `config migrate` |
| [docs/reverse-proxy.md](docs/reverse-proxy.md) | Paths, TLS, trusted proxies, timeouts, Traefik / Caddy / nginx |
| [docs/clients/outlook-mac.md](docs/clients/outlook-mac.md) | Outlook for Mac setup and behavior |
| [docs/clients/outlook-mobile.md](docs/clients/outlook-mobile.md) | Outlook for iOS / Android setup and behavior |
| [docs/operations.md](docs/operations.md) | Logs, check, backup, restore, upgrade, rollback, reset, key rotation |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Passwords / 2FA, TLS, Autodiscover, status codes, buffering, logs |
| [docs/limitations.md](docs/limitations.md) | All functional and consistency limits |
| [docs/privacy.md](docs/privacy.md) | Data flows, logs, Microsoft cloud, operator duties |
| [docs/architecture.md](docs/architecture.md) | Components, state, conflicts, threat model |
| [docs/protocol-coverage.md](docs/protocol-coverage.md) | What each EWS operation / EAS command does |
| [docs/protocol-sources.md](docs/protocol-sources.md) | Specifications used and where they are implemented |
| [examples/](examples/) | Compose file and reverse proxy configurations |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |
| [SECURITY.md](SECURITY.md) | Reporting vulnerabilities (private) |
| [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Contributing |
| [NOTICE](NOTICE), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), [LICENSE](LICENSE) | Legal |

## Support

There is none promised. Open an [issue](https://github.com/chr0mcom/davbridge/issues) for bugs (use the template and never post secrets or
calendar data); report vulnerabilities privately via [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE). Third-party notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). See [NOTICE](NOTICE) for the Microsoft documentation
material, trademark and patent statements.
