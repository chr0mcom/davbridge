# Installation

This guide takes you from nothing to a working Outlook for Mac connection in about 30 minutes. For Outlook mobile,
read [privacy.md](privacy.md) first.

## Requirements

| Requirement | Details |
|---|---|
| Host | Linux with Docker Engine and the Compose plugin; `linux/amd64` or `linux/arm64`. About 400 MB RAM for the container. |
| **Public DNS name** | An `A`/`AAAA` record such as `dav.example.com` pointing at your reverse proxy. Optional: `autodiscover.dav.example.com` for automatic account setup on the address domain. |
| **Publicly trusted TLS certificate** | Full chain, correct host name (all names clients use), TLS 1.2 or newer. Self-signed certificates and private CAs are not supported (Outlook mobile never accepts them; Outlook for Mac rejects them in practice). |
| Reverse proxy | Traefik, Caddy, nginx or similar, configured as in [reverse-proxy.md](reverse-proxy.md). |
| CalDAV server | sabre/dav-based (Nextcloud is the tested one), reachable **from the container** over HTTPS with a valid certificate. |
| Credentials | One CalDAV **app password** per client device. Mandatory when two-factor authentication is on; recommended always. |
| Reachability | Outlook for Mac: only from the Mac. Outlook mobile: from the **public internet** (Microsoft's service connects), without IP allow-lists, geo-blocking, VPN-only access or SSO in front. |

Create an app password in Nextcloud under *Personal settings -> Security -> Devices & sessions*, name it
(for example `davbridge-mac`), and copy it once. If your CalDAV server offers it, disable file-system access for that
password; DAVbridge only needs CalDAV.

## 1. Prepare a directory

```bash
mkdir -p ~/davbridge && cd ~/davbridge
curl -fsSLO https://raw.githubusercontent.com/chr0mcom/davbridge/v0.1.0/examples/compose/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/chr0mcom/davbridge/v0.1.0/config/davbridge.example.toml -o davbridge.toml
chmod 644 davbridge.toml    # holds no secrets; the container user (UID 65532) must be able to read it, see "File permissions" below
```

Optional but recommended: pin the image by digest.

```bash
docker pull ghcr.io/chr0mcom/davbridge:0.1.0
docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/chr0mcom/davbridge:0.1.0
# then use  image: ghcr.io/chr0mcom/davbridge:0.1.0@sha256:<digest>  in docker-compose.yml
```

Do not use `latest` in production; upgrades should be deliberate ([operations.md](operations.md#upgrade)).

## 2. Edit the configuration

At minimum change `server.public_url`, `caldav.base_url`, `server.trusted_proxies` (your proxy's address), the user
(`allowed_users` and the `[[users]]` block: `username`, `address`, calendar `path`) and `address_template`. Keep
`auth.mode = "allowlist"`, `protocols.eas.enabled = false`. Everything is explained in [configuration.md](configuration.md).

Find the calendar `path` in your CalDAV server: the last path segment of the calendar URL, e.g.
`https://cloud.example.com/remote.php/dav/calendars/alice/personal/` -> `personal`.

## 3. Initialize

```bash
docker compose run --rm davbridge davbridge init
```

`init` creates `hmac.key` (32 random bytes, Base64, mode `0600`) atomically in the state directory (creating the directory if needed), never overwrites an existing key and creates **no**
example users. It is run once before the first start. Back up `hmac.key` like a secret. The service and `davbridge check` accept the key only as a regular file (no symbolic link) that belongs to the user running DAVbridge and has no group or other permission bits (`0600`); otherwise `serve` exits with code 2 and `check` reports an error.

### File permissions

The container runs as the fixed, nameless UID/GID **65532:65532** with a read-only root file system. The named volume
`davbridge-state` is initialized with the right owner automatically. If you use a **bind mount** instead, give UID 65532
write access to exactly that directory:

```bash
mkdir -p ./state && sudo chown 65532:65532 ./state && sudo chmod 700 ./state
# in docker-compose.yml:  - ./state:/var/lib/davbridge
```

The configuration file is mounted read-only and must be readable by UID 65532: mode `644` (it contains no secrets), or `sudo chown 65532:65532 davbridge.toml` with mode `600`.

## 4. Check

```bash
docker compose run --rm davbridge davbridge check
read -rsp "App password for alice: " PW; echo
printf '%s' "$PW" | docker compose run --rm -T davbridge davbridge check --user alice --password-stdin; unset PW
```

The first command validates the configuration, shows whether EWS and EAS are on and whether the mobile-cloud acknowledgement is set, checks the state directory
(writable, permissions, free space, `hmac.key`, database integrity and schema) and the CalDAV server (reachable, TLS verified, no redirect). The second also
signs the user in and checks every configured calendar (exists, supports events, effective rights) - or lists the discovered candidates and the default calendar for users with
autodiscovery - **without reading events**. Exit codes: `0` no warnings or errors, `1` warnings, `2` problems that prevent operation. `--json` gives a machine-readable report.

## 5. Start and connect the proxy

```bash
docker compose up -d
docker compose ps          # state "healthy" after ~20 s
```

The container runs `davbridge serve` (exactly one uvicorn worker bound to `server.listen`) and its health check runs `davbridge healthcheck`, which probes `/healthz` on that port.

The compose example attaches the service to an existing Docker network named `proxy` (`networks.proxy.name`, change it to the network of your reverse proxy; create one with
`docker network create proxy` if you have none). Attach your reverse proxy to the same network and use one of the examples in
[`examples/reverse-proxy/`](../examples/reverse-proxy/) (see [reverse-proxy.md](reverse-proxy.md)). The service is reachable as `davbridge:8000` on that network only; it publishes no port.

Then tell DAVbridge which peer is your proxy (`server.trusted_proxies`), otherwise every client appears with the proxy's address and ten failed logins from anyone lock everybody out:

```bash
docker network inspect proxy --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}'              # the subnet of the network
docker inspect <proxy-container> --format '{{range $name, $n := .NetworkSettings.Networks}}{{$name}} {{$n.IPAddress}}{{"\n"}}{{end}}'   # the proxy's address on it
```

Use the proxy's own address as a single-host entry (`"172.18.0.2/32"`), not the subnet. A container address can change when the container is recreated: give the proxy a fixed address
(`ipv4_address` in its compose file on a network with a `subnet`), or put it into a small dedicated network (for example a `/29`) that contains nothing else, and list that
narrow subnet. Never list a whole shared network such as the default bridge. Edit `davbridge.toml`, run `davbridge config validate`, then `docker compose restart davbridge`.

Then:

```bash
docker compose exec davbridge davbridge check --public
```

This requests your public URL with certificate verification (chain and host name), confirms that unauthenticated requests to the EWS and EAS routes get `401`
with a plain `Basic` challenge, unknown paths `404` and that `/healthz` is not publicly routable. It cannot detect buffering or compression; for that use
`tools/stream_probe.sh` ([reverse-proxy.md](reverse-proxy.md#verify)). Note: a check from the same host does **not**
prove reachability from the internet. Test from outside as well (for example from your phone on mobile data, or an external HTTPS checker).

## 6. Connect a client

- [Outlook for Mac](clients/outlook-mac.md)
- [Outlook for iOS / Android](clients/outlook-mobile.md) (read the privacy warning first)

## 30-minute acceptance checklist

| # | Check | Done |
|---|---|---|
| 1 | DNS resolves; `https://dav.example.com/` shows a valid certificate chain in a browser (a 404 is expected) | [ ] |
| 2 | `davbridge check` exits `0` (or only expected warnings) | [ ] |
| 3 | `davbridge check --user alice --password-stdin` lists the calendar and shows write permission | [ ] |
| 4 | `davbridge check --public` is clean | [ ] |
| 5 | `docker compose ps` shows `healthy`; no restart loop | [ ] |
| 6 | Outlook for Mac account added; the calendar appears with your existing events | [ ] |
| 7 | Create an event in Outlook -> it appears in Nextcloud within seconds | [ ] |
| 8 | Change that event in Nextcloud -> right-click calendar -> *Synchronize* -> Outlook shows the change | [ ] |
| 9 | Delete the test event in Outlook -> gone in Nextcloud | [ ] |
| 10 | `docker compose logs davbridge` shows no errors and **no calendar content** | [ ] |
| 11 | Backup taken: `davbridge backup --output ...` ([operations.md](operations.md#backup)) | [ ] |

Use a throw-away test event only. If something fails, see [troubleshooting.md](troubleshooting.md).

## Hardening reminders

Public TLS only; app passwords per device; keep the `allowlist`; narrow `trusted_proxies`; never publish `/healthz`; no
Docker socket; do not scale the service beyond one container; keep the image current.
