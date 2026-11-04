# Security policy

DAVbridge is a network-facing service that handles calendar data and (in memory only)
CalDAV credentials. We take security reports seriously, but this is a volunteer-run,
unfunded project: there is **no SLA and no support commitment**. Reports are handled on a
best-effort basis.

## Supported versions

| Version | Supported |
|---|---|
| Latest stable minor release line (for example `0.1.x`) | Yes, best effort |
| Older minor lines, pre-releases (`-rc.N`), unreleased `main` | No |

Before 1.0, a new minor release may contain incompatible configuration, CLI or state changes
(see [CHANGELOG.md](CHANGELOG.md)). Security fixes are published as a patch release of the latest
minor line and, if the fix requires incompatible changes, as a new minor release.

## Verifying a release

Release tags are **not cryptographically verified yet**: the release workflow checks the tag format, that the commit is on `main`, the version and the changelog, but it does not require or check
a signed tag. Do not treat a tag as proof of origin. The verification anchors are the **published image digest** (`ghcr.io/chr0mcom/davbridge@sha256:...`, also in the release notes and in the
asset `IMAGE_DIGEST.txt`) and the **GitHub release assets**: `SHA256SUMS` (wheel, SBOMs, digest file), the SPDX SBOMs, and the build provenance attestation that is attached to the image digest
in the registry (`docker buildx imagetools inspect <image>@<digest>`). Pin the digest when you deploy.

## Reporting a vulnerability

**Please report vulnerabilities privately** using GitHub's private vulnerability reporting:

> <https://github.com/chr0mcom/davbridge/security/advisories/new>
> (repository page → **Security** tab → **Report a vulnerability**)

Please include:

- the affected version (`davbridge version`) and how you run DAVbridge (image tag or digest);
- a description of the issue and its impact;
- steps to reproduce, ideally with **synthetic** data and a minimal configuration;
- whether the issue is already public.

**Do not** open a public issue or discussion for a suspected vulnerability, and
**never post secrets or real calendar data** anywhere in this repository - not in issues,
pull requests, logs, screenshots or test fixtures. If you accidentally published a credential,
revoke it first (for example the CalDAV app password), then tell us.

No security e-mail address is published for this project; GitHub private reporting is the only
supported channel.

## What to expect

1. We acknowledge the report when a maintainer sees it (no guaranteed response time).
2. We reproduce and assess severity and affected versions.
3. We prepare a fix and tests privately (GitHub private fork / security advisory).
4. If appropriate, we coordinate a CVE or GitHub Security Advisory.
5. We publish a release with the fix and mitigation advice and only then disclose the details.
   Reporters are credited if they wish.

## Scope

In scope: the DAVbridge server and CLI, the container image and the example configurations in this
repository (for example authentication bypass, request smuggling through the documented proxy
setup, credential leakage into logs or state, SSRF through CalDAV discovery, path traversal in
CalDAV hrefs, denial of service by a single unauthenticated request).

Out of scope: vulnerabilities in Microsoft Outlook, Microsoft's cloud services, Nextcloud, other
CalDAV servers, reverse proxies or the operating system (please report those to the respective
vendors); misconfiguration that the documentation warns against (for example `auth.mode = "pattern"`
on an internet-facing server, or exposing `/healthz`); and the limitations listed in
[docs/limitations.md](docs/limitations.md).

## Operator hardening checklist

See [docs/installation.md](docs/installation.md), [docs/reverse-proxy.md](docs/reverse-proxy.md) and
[docs/operations.md](docs/operations.md). In short: publicly trusted TLS only, use CalDAV app
passwords (one per client device) that you can revoke, keep the default `allowlist` mode,
keep `server.trusted_proxies` as narrow as possible, do not route `/healthz` publicly, keep the
container read-only and non-root, and back up `hmac.key` like a secret.
