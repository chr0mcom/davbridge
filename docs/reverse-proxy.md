# Reverse proxy

DAVbridge speaks plain HTTP on port 8000 inside your Docker network. A reverse proxy terminates TLS and exposes **only** the
protocol paths. Complete, syntax-checked examples are in [`examples/reverse-proxy/`](../examples/reverse-proxy/):
[`traefik.yml`](../examples/reverse-proxy/traefik.yml), [`Caddyfile`](../examples/reverse-proxy/Caddyfile),
[`nginx.conf`](../examples/reverse-proxy/nginx.conf).

## Paths to route

Route these **without changing the path**, case-insensitively, to `http://davbridge:8000`:

| Path | Purpose |
|---|---|
| `/EWS/Exchange.asmx` | Exchange Web Services (Outlook for Mac) |
| `/autodiscover/autodiscover.xml` | Autodiscover (POX) for Outlook for Mac and Outlook mobile |
| `/autodiscover/autodiscover.json` and `/autodiscover/autodiscover.json/v1.0/<address>` | Autodiscover JSON v2 (Outlook mobile, only when EAS is enabled) |
| `/Microsoft-Server-ActiveSync` (optional trailing `/`) | Exchange ActiveSync (Outlook mobile, only when EAS is enabled) |

Everything else must return 404 (the examples do this). **`/healthz` stays internal.** Never route diagnostics, reset or
admin functions; there is no web interface.

## TLS

- Publicly trusted certificate with the complete chain and the correct names (also `autodiscover.<domain>` if you use it).
- TLS 1.2 or newer; HTTP/1.1 and HTTP/2 towards clients are fine.
- Redirect port 80 to HTTPS; send HSTS.
- Pass the `Authorization` header unchanged and set `Host`.

## Trusted proxies and client IP

DAVbridge uses the client IP for lock-outs. It honors `X-Forwarded-For` / `X-Forwarded-Proto` **only** from peers listed in
`server.trusted_proxies` (default: none). Put the address (CIDR) of **your** proxy there, as narrow as possible - never a
whole shared network you do not control, and never `0.0.0.0/0` or `::/0` (a `/0` network is rejected as a configuration error). Otherwise lock-outs can be forged, or all clients appear as one
address and lock each other out.

If another proxy or CDN sits in front of yours, make your proxy trust it (nginx `set_real_ip_from`, Caddy `trusted_proxies`,
Traefik `forwardedHeaders.trustedIPs`), and keep the DAVbridge list to your own proxy. How to find the proxy's address and subnet (`docker network inspect`) is described
in [installation.md](installation.md#5-start-and-connect-the-proxy).

## Streaming, long polling, timeouts

| Setting | Requirement | Why |
|---|---|---|
| Upstream protocol | HTTP/1.1 | Streams and long requests |
| **Response buffering** | **off** | EWS streaming notifications must be flushed immediately |
| **Compression** | **off** (no gzip/brotli/zstd, no `compress` middleware) | Compression buffers and delays streamed chunks |
| Flush | immediate (`flush_interval -1`, `flushInterval: -1ms`, `proxy_buffering off`) | |
| Response / read timeout | **>= 1900 s** | An EWS stream lasts up to `stream_max_seconds` (1800 s) |
| EAS Ping | **>= 650 s** | The Ping response arrives when the heartbeat (up to 540 s) ends |
| Request buffering | may be off | |
| Max request size | **1 MiB** | DAVbridge refuses larger bodies with `413` |
| Response-body inspection / rewriting plugins | none | |

If you reduce `protocols.ews.stream_max_seconds`, the proxy timeout must stay at least that value plus 100 s.

## Authentication and security in front

**Do not put anything in front that challenges the client**: no basic-auth portal, SSO / OIDC forward-auth, captcha, bot
protection with JavaScript challenge, mTLS requirement, `Bearer` challenge (`WWW-Authenticate: Bearer` makes Outlook
mobile try an unsupported Modern Authentication path). DAVbridge answers `401` with `WWW-Authenticate: Basic` itself and
lock-outs happen there. IP allow-lists work for Outlook for Mac but break Outlook mobile.

**Rate limits** must permit a full first synchronization (hundreds of requests). Starting values: **20 requests/s, burst 60
per real client IP** (the nginx and Traefik examples set exactly this). Core Caddy has no rate-limit directive, so the Caddy example has none: add the third-party `caddy-ratelimit` module with the same values if you want it there. Do not enable web-application-firewall rules that inspect or block SOAP/WBXML bodies.

Add security headers (HSTS, `X-Content-Type-Options`, ...); the examples include them.

## Traefik

The example is a *dynamic* configuration (file provider). Static configuration requirements:

```yaml
# traefik static configuration (excerpt)
entryPoints:
  web:
    address: ":80"
    http:
      redirections:
        entryPoint: {to: websecure, scheme: https}
  websecure:
    address: ":443"
    # Do not set respondingTimeouts.writeTimeout (default 0 = none); a value below 1900s would cut streams.
    # Optional but recommended when Traefik is behind another trusted proxy:
    # forwardedHeaders: {trustedIPs: ["203.0.113.0/24"]}
certificatesResolvers:
  letsencrypt:
    acme:
      email: you@example.com
      storage: /letsencrypt/acme.json
      httpChallenge: {entryPoint: web}
providers:
  file: {directory: /etc/traefik/dynamic, watch: true}
```

Traefik's `buffering` middleware also buffers responses and breaks streaming - never use it here. Traefik cannot cap the
request size without it; DAVbridge enforces the 1 MiB limit itself.

## Caddy

See [`Caddyfile`](../examples/reverse-proxy/Caddyfile): matcher by regular expression, `flush_interval -1`,
`versions 1.1`, `compression off`, `read_timeout 1900s`, `request_body max_size 1MiB`. Do not add `encode`.

## nginx

See [`nginx.conf`](../examples/reverse-proxy/nginx.conf): `proxy_http_version 1.1`, `proxy_buffering off`,
`proxy_request_buffering off`, `gzip off`, `proxy_read_timeout 1900s`, `client_max_body_size 1m`, `limit_req` 20r/s burst 60.

## Verify

```bash
docker compose exec davbridge davbridge check --public
printf '%s' "$PW" | tools/stream_probe.sh --url https://dav.example.com --user alice --password-stdin --minutes 2   # streaming test through the proxy
```

`check --public` verifies the anonymous answers of the public routes (401 with a Basic challenge, 404 for unknown paths, `/healthz` not routable). It cannot see
buffering or compression: `tools/stream_probe.sh` (in the repository; needs `curl`) opens a real streaming request through the proxy with credentials of a **test user**
and shows whether keep-alives arrive in time. Also test from the internet side. Common symptoms and fixes: [troubleshooting.md](troubleshooting.md#proxy-buffering-and-timeouts).
