# Troubleshooting

Start with `docker compose exec davbridge davbridge check --json` and `davbridge check --public` (both read-only). Never post secrets or calendar
content when asking for help ([SECURITY.md](../SECURITY.md)).

## Quick index

| Symptom | Section |
|---|---|
| Outlook rejects the password | [Wrong password / 2FA](#wrong-password-or-two-factor-authentication) |
| Certificate or connection errors | [TLS chain](#tls-chain-and-certificate-errors) |
| "Account cannot be found", server not detected | [Autodiscover](#autodiscover) |
| HTTP 401 / 403 / 503 / 400 / 413 / 501 | [Status codes](#http-status-codes) |
| Mobile setup keeps repeating | [Setup never finishes](#outlook-mobile-setup-never-finishes) |
| Notifications late or streams drop | [Proxy buffering and timeouts](#proxy-buffering-and-timeouts) |
| Mac does not show Nextcloud changes | [Stale subscriptions](#stale-ews-subscriptions-and-missing-updates) |
| All clients blocked | [Lock-outs](#everybody-is-locked-out-or-503-for-all-clients) |
| Understanding log lines | [Structured logs](#reading-the-structured-logs) |

## Wrong password or two-factor authentication

If two-factor authentication is on, your account password **never** works for CalDAV clients. Create an **app password** (Nextcloud:
*Personal settings -> Security -> Devices & sessions*) and use that in Outlook. Also check:

- The user name is the CalDAV login (`alice`), not the display name. The address (`alice@dav.example.com`) also works if it is configured.
- The user is in `allowed_users`/`[[users]]`. An unknown user is answered exactly like a wrong password (`401`) so attackers cannot probe
  for accounts; the log reason tells you which it was (`unknown-user` vs `wrong-password`).
- The app password was not revoked. A revoked password is cached for at most `auth.positive_cache_seconds` (default 5 minutes).
- After repeated failures your IP is locked for a while ([Lock-outs](#everybody-is-locked-out-or-503-for-all-clients)).
- Test the same credentials with `printf '%s' "$PW" | docker compose exec -T davbridge davbridge check --user alice --password-stdin`; it says whether the CalDAV server rejects them
  and never prints the password. In pattern mode the user name must match `auth.username_pattern` exactly, otherwise it is refused as `unknown-user` without contacting CalDAV.

## TLS chain and certificate errors

Outlook for Mac and especially Microsoft's cloud for Outlook mobile need a **publicly trusted certificate with the complete
chain** and a matching name. Typical causes: only the leaf certificate is served (use `fullchain.pem`), expired certificate,
wrong host name, a self-signed or private-CA certificate, an old TLS version. Check with a browser, `openssl s_client -connect dav.example.com:443 -servername dav.example.com`
or `davbridge check --public` (it fails if the certificate chain or host name cannot be verified; it does not print the expiry date). For the CalDAV side
(container -> Nextcloud) the same applies; `verify_tls = true` rejects invalid certificates, and `davbridge check` reports "TLS verification failed" when the CalDAV server's certificate is not accepted.

## Autodiscover

Outlook looks for `https://<address domain>/autodiscover/autodiscover.xml` and `https://autodiscover.<address domain>/autodiscover/autodiscover.xml`.

- The domain of the account address (`alice@dav.example.com` -> `dav.example.com`) must resolve to DAVbridge's proxy; `autodiscover.<domain>` too if you
  want that name to work, and the certificate must cover it.
- The route must match the path case-insensitively (see [reverse-proxy.md](reverse-proxy.md)).
- DAVbridge only answers for configured domains and returns the account's own address; asking for a different address yields an error.
- No Autodiscover? Enter the server manually ([Mac](clients/outlook-mac.md), [mobile](clients/outlook-mobile.md)).
- Proxy layers that challenge unauthenticated requests (SSO, basic-auth portal, captcha) break Autodiscover; see [reverse-proxy.md](reverse-proxy.md).

## HTTP status codes

| Code | Meaning | What to do |
|---|---|---|
| `401` + `WWW-Authenticate: Basic` | Missing or wrong credentials, unknown user, or an anonymous probe (expected for `check`). Outlook mobile first calls anonymously, then with credentials. | [Wrong password](#wrong-password-or-two-factor-authentication) |
| `403` (EAS: "user is not enabled for ActiveSync") | The user is valid but `eas_enabled` is not set, or EAS is not enabled | [Double opt-in](configuration.md#outlook-mobile-eas-double-opt-in) |
| `403` without a message, no `WWW-Authenticate` | Calendar autodiscovery found no usable calendar or cannot choose the default calendar (log: `login_rejected`, `reason` `no-default-calendar`) | [Calendar discovery](configuration.md#calendar-discovery); `davbridge check --user ... --password-stdin` lists the candidates |
| `503` + `Retry-After` | (a) CalDAV server unreachable or erroring (30 s); (b) your client IP is locked after failed logins (up to 900 s); (c) too many concurrent EAS requests for one user (5 s) or too many waiting pings (30 s); (d) request timeout | Check `davbridge check`, the CalDAV server, [lock-outs](#everybody-is-locked-out-or-503-for-all-clients) |
| `400` | EAS protocol version other than 14.1, unknown command, malformed WBXML, wrong content type | Client too new/old; look for `version_rejected` in the log |
| `413` | Request body larger than the limit (1 MiB; configurable lower limits for EAS) | Normally a proxy that merged or buffered requests, or a broken client |
| `404` | Unknown path, or EAS/mobile Autodiscover when EAS is disabled | Check routes and `protocols.eas.enabled` |
| `405` | Method other than `OPTIONS`/`POST`/`GET` on the EAS path | - |
| `500` | Unexpected error (the log shows the exception **type** only); EWS malformed requests return a SOAP fault | Report with `check --json` and the log line |
| `501` | An EAS command that DAVbridge does not advertise | Client probing; ignore unless setup fails |

## Outlook mobile setup never finishes

During setup Outlook mobile runs `Provision`, `FolderSync`, then **`FolderCreate` for a custom mail folder (type 12)**, then `Sync` and
`ItemOperations`. If the server refuses that `FolderCreate`, the app repeats the whole sequence forever and never synchronizes.
DAVbridge therefore accepts a type-12 folder as an **always-empty, per-device compatibility folder**; every other folder type (calendar,
tasks, ...) is still refused with status 3. If you still see a loop:

1. `docker compose logs davbridge` - is there a `folder_created` event followed by `eas_request` records for `Sync`? If requests stop at `FolderCreate`, an old image or a proxy
   problem is likely.
2. Make sure the account is enabled for EAS (`403` otherwise) and the acknowledgement is set.
3. The server must be reachable from the Microsoft cloud, not only from your phone's Wi-Fi.
4. `docker compose exec davbridge davbridge reset devices --user alice`, remove the account in the app, add it again.
5. Check the Ping timeout in your proxy (>= 650 s) and that nothing buffers.

## Proxy buffering and timeouts

Symptoms: Outlook for Mac streams end after 30-60 seconds with
`504`/`502`; mobile pings return immediately or fail; "Nextcloud changes arrive minutes late".

Causes and fixes: response buffering on (nginx `proxy_buffering off`, Traefik: no `buffering` middleware, `flushInterval: -1ms`, Caddy `flush_interval -1`),
compression on (disable gzip/brotli/`compress`), read timeout below 1900 s (EWS) / 650 s (EAS Ping), HTTP/2-to-HTTP/1.1 idle timeouts of
a CDN in front, a WAF that buffers bodies. Use the example configurations unchanged first.

## Stale EWS subscriptions and missing updates

EWS subscriptions exist only in memory. After a restart or upgrade Outlook for Mac keeps calling with the old subscription ID and gets
`ErrorSubscriptionNotFound` (answered with a short delay; this is normal and not a loop). Outlook does not resubscribe by itself. Use
right-click on the calendar -> **Synchronize** or restart Outlook. Also remember that Outlook for Mac does not fetch the calendar on
streaming events, so Nextcloud changes need **Synchronize** anyway ([limitations.md](limitations.md#freshness-changes-made-outside-outlook)).

## Everybody is locked out, or 503 for all clients

If lock-outs hit unrelated clients, DAVbridge sees the proxy's IP for every request: put the proxy's address into `server.trusted_proxies`, and make
the proxy send `X-Forwarded-For`. Unblock afterwards: `davbridge unblock --all`. Forged `X-Forwarded-For` values from untrusted peers are ignored by design.

## Reading the structured logs

Each line is a JSON object. Typical fields (the set is a fixed allow-list; unknown fields are dropped):

| Field | Meaning |
|---|---|
| `time` | UTC timestamp |
| `event` | Kind of record (always lower case with underscores), for example `request` (EWS operation), `eas_request` (EAS command), `login_rejected`, `folder_created`, `eas_ping_end`, `change_rejected`, `eas_conflict`, `stream_end`, `raw_request` (an EWS or Autodiscover request with its status and authentication scheme, rejected ones included; never the credentials) |
| `operation` / `command` | EWS operation or EAS command name |
| `user` | CalDAV user name |
| `codes` | EWS `ResponseCode` names or EAS status numbers returned |
| `count` | Number of items handled |
| `duration_ms` | Processing time |
| `http_status` | HTTP status returned |
| `reason` | Why something was rejected, for example `invalid-authorization`, `unknown-user`, `wrong-password`, `wrong-password-cached`, `locked`, `throttled`, `caldav-unavailable`, `no-default-calendar`; for refused changes `read-only`, `protected-property`, `conflict`, `delete-brake`, `series`, `attendees` |
| `device`, `device_type`, `agent` | Hashed EAS device ID, device type, client name/version |
| `request_id` | Correlates records of one request |
| `item`, `folder`, `etag_hash` | Hashed or signed identifiers, never raw values |

Example (illustrative):

```json
{"time":"2026-11-04T10:15:02.123+00:00","event":"request","operation":"SyncFolderItems","user":"alice","codes":["NoError"],"count":3,"duration_ms":41,"request_id":"3f9c1b2a7d40"}
{"time":"2026-11-04T10:15:09.554+00:00","event":"login_rejected","reason":"wrong-password"}
{"time":"2026-11-04T10:16:00.210+00:00","event":"change_rejected","reason":"conflict","request_id":"a81f00c2e9b1"}
```

Useful filters: `docker compose logs davbridge | grep '"login_rejected"'` (credential problems), `... | grep '"change_rejected"'` (why an edit was refused),
`... | grep -v '"NoError"'` (EWS errors). A missing field means it was not valid for the allow-list, not that it was hidden for other reasons.

## Still stuck?

Search existing issues, then open a bug report with the output of `davbridge check --json`, versions of client, proxy and CalDAV server, and a few
redacted log lines.
