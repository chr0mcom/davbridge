# Contributing to DAVbridge

Thank you for your interest! DAVbridge is a small, volunteer-run project. Bug reports, documentation
fixes, test cases and focused pull requests are welcome. Please read this page before you start.

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ground rules

- **Language:** everything user-visible and everything in the repository is **English** - code,
  identifiers, comments, docstrings, configuration keys, CLI output, log events, error messages,
  documentation, commit messages and issues. Protocol tokens (XML/WBXML tags, EWS response codes, HTTP
  headers, iCalendar properties, time zone IDs) keep their standard names.
- **No real data:** never commit or post real calendar data, real names, e-mail addresses, domains,
  credentials, tokens, private keys or logs from a real deployment. Use `example.com`, `alice`,
  and similar synthetic values. Do not attach network captures of Microsoft clients or services:
  proprietary captures and traces of third-party servers are not accepted. A protocol skeleton
  (structure without content) produced by your own test client with synthetic data is fine.
- **No copied specification text:** do not paste text, schemas or tables from Microsoft documents (beyond
  short protocol identifiers) into code, comments or docs. Describe the behavior in your own words
  and cite the document and section in [docs/protocol-sources.md](docs/protocol-sources.md).
  Changes to the transcribed token tables need an explicit maintainer review.
- **No claims without tests:** documentation must describe what the code and the tests actually do. When
  you add or change protocol behavior, update [docs/protocol-coverage.md](docs/protocol-coverage.md) and
  [docs/limitations.md](docs/limitations.md) in the same pull request.
- **No product logos or trademarks as project assets.** See [NOTICE](NOTICE).

## Design constraints you should know

These are intentional and are enforced by architecture tests; please do not "improve" them in a
drive-by change (see [docs/architecture.md](docs/architecture.md)):

- exactly one server process with one event loop and one uvicorn worker;
- CalDAV is the only source of truth; the SQLite state never stores event content;
- no blocking I/O in request handlers; SQLite access only in the store module; HTTP access to CalDAV only
  in the CalDAV module;
- conditional writes (`If-Match`) based on the ETag the client last saw;
- lossless patching of existing iCalendar objects (unknown properties, alarms and `VTIMEZONE` survive);
- log and diagnostic output uses allow-lists; it never contains credentials, event content, raw device IDs
  or raw request bodies.

## Development setup

Requirements: Python 3.12, Git, Docker (only for the image and integration tests).

```bash
git clone https://github.com/chr0mcom/davbridge.git
cd davbridge
python3.12 -m venv .venv
. .venv/bin/activate
pip install --require-hashes --only-binary=:all: -r requirements/dev.txt -r requirements/build.txt   # runtime, test and lint tools, build tools
pip install --no-index --no-build-isolation --no-deps -e .                                          # the project itself
```

Every install goes through a lock file with hashes (`requirements/runtime.txt`, `build.txt`, `dev.txt`, generated from the matching `*.in` file; the command
is in the header of each `.in` file). To change a dependency, edit the `.in` file and `pyproject.toml`, regenerate the locks with `pip-compile --generate-hashes
--allow-unsafe --strip-extras` (pip-tools) and commit all of them. The wheel is built with `pip wheel --no-index --no-build-isolation --no-deps` from an
environment that contains exactly `requirements/build.txt`.

### Tests

```bash
pytest                          # complete offline suite, no network, no real accounts
pytest -m integration           # needs a disposable Nextcloud with a synthetic user (see below)
```

The offline suite is what CI runs on every push and pull request. It must pass, together with the
linters:

```bash
ruff check src tests tools
ruff format --check src tests tools     # use `ruff format src tests tools` to fix
```

A documentation consistency test (`tests/unit/test_docs.py`) fails when a CLI subcommand is missing from the docs, when a key of the example configuration is unknown to the
schema, or when the documented configuration keys, environment variables and log event names drift from the code. Update the documents together with the code.

Integration tests (`tests/integration/`) talk to a real CalDAV server. Run them **only against a throw-away
instance with a synthetic user and calendar**, never against a server that holds real data. The manual
GitHub workflow `integration.yml` does this with a temporary Nextcloud container.

### Container image

```bash
docker build -t davbridge:dev --build-arg VERSION=0.1.0-dev --build-arg REVISION="$(git rev-parse HEAD)" .
docker run --rm --read-only --user 65532:65532 --tmpfs /tmp davbridge:dev davbridge version
```

The image is a multi-stage build (hash-pinned dependencies and the project wheel, built with hash-pinned build tools, in a virtual environment that is copied onto the same base image), runs as UID/GID 65532,
keeps all state in the `/var/lib/davbridge` volume and runs `davbridge serve` with a `davbridge healthcheck` health check. The release workflow builds `linux/amd64` and `linux/arm64`
from this Dockerfile.

### Releases

Releases are built by `.github/workflows/release.yml` from version tags (`vX.Y.Z`, `vX.Y.Z-rc.N`) that the maintainer pushes for a commit on `main`; the workflow checks the tag
format, that the commit is on `main`, the version in `pyproject.toml` and the section in `CHANGELOG.md`. The image is first pushed only under the staging tag `sha-<full commit SHA>`
(the workflow refuses to overwrite an existing one; only the image digest is immutable), tested and scanned by digest (the amd64 and the arm64 variant separately), and only then promoted to `X.Y.Z` (and `X.Y`, `X`, `latest` for stable versions). The GitHub release carries the wheel, the SPDX SBOMs and checksums.

**Before the first release / repository prerequisites** (maintainer, once; none of this is created by the workflows):

- [ ] Enable **private vulnerability reporting** (Settings -> Advanced Security). Until then the advisory links in `SECURITY.md`, `CODE_OF_CONDUCT.md` and the issue templates lead to a 404.
- [ ] Create the **labels** that the issue templates and Dependabot apply: `bug`, `enhancement`, `needs-triage`, `dependencies`, `python`, `docker` and `github-actions`.
- [ ] Set the repository secret **`DAVBRIDGE_PRIVATE_TERMS`** (Settings -> Secrets and variables -> Actions): the maintainer-only list of terms that must never appear in the repository.
  Its content is known to the maintainers only and is never written down in the repository. The release workflow requires it (`require_terms: true`): without it the release fails before anything is published.
  Pull requests from forks have no access to secrets and skip that check with a notice.
- [ ] After the **first** release, make the GHCR package `ghcr.io/chr0mcom/davbridge` **public** (package settings -> Change visibility). A new package is private, so `docker pull` of the documented image
  fails until then.
- [ ] Know the **CVE gate**: the release workflow scans both platform variants of the built digest with Trivy and fails on any *fixable* HIGH or CRITICAL finding. The run then stops before the version tags are
  promoted; only the staging tag `sha-<full commit SHA>` exists in the registry. Fix the cause (update the base image or the dependencies) and release a new version; to repeat a run for the same commit, delete its staging tag in the package settings first.

### Test tools

The scripts in `tools/` are developer aids, not part of the stable `davbridge` command. They only talk to servers and accounts that you name explicitly, and read passwords from
standard input (`--password-stdin`), never from arguments or the environment. Use **throw-away test accounts only**.

- `tools/eas_test_client.py` - independent Exchange ActiveSync 14.1 client (standard library only, its own WBXML codec) that runs the scenarios T1-T24 against a running DAVbridge.
  Required options: `--url BASE_URL --user USER --password-stdin`. Useful options: `--device ID`, `--folder NAME` (calendar id or part of its name, default `Test Calendar`),
  `--folder-read-only NAME`, `--state FILE` (sync keys and server ids, default `~/.cache/eas-testclient/<user>.json`, mode 0600), `--timeout SECONDS`, `--tls-insecure`, `--quiet`, `-v` / `--verbose`.
  Subcommands: `options`, `autodiscover`, `provision`, `foldersync`, `settings`, `sync-null`, `sync-full [--window N]`, `add [--all-day]`, `change`, `delete`, `retry`, `keys`, `wake`,
  `ping [--heartbeat N]`, `ping-limits`, `ping-replaced`, `readonly`, `series`, `rejected`, `unknown`, `password`, `lockout --confirm-lock`, `load`, `concurrent`, `restart`, `extra`,
  `cleanup`, `clear-state` and `all [--window N] [--fast]` (T1-T21 in sequence with PASS/FAIL/SKIP). Test events carry the subject prefix `DAVBRIDGE-TEST-`.
  Scenario options: `wake` and `restart` take `--trigger COMMAND` (a second actor, for example a command that creates an event within seconds; `restart` also `--delay SECONDS`);
  `ping` and `wake` take `--heartbeat SECONDS`; `load` takes `--window N`, `--max-duration SECONDS`, `--expect-series N`, `--expect-private N`, `--expect-count N` and
  `--rss-measure --container NAME --rss-limit MB` (reads `docker stats`); `concurrent` takes `--window N` and `--repeats N`.
  Example: `printf '%s' "$PW" | python tools/eas_test_client.py all --fast --url https://dav.example.com --user test-user --password-stdin`.
- `tools/stream_probe.sh` - shows the arrival time of EWS streaming chunks through a reverse proxy (`--url`, `--user`, `--password-stdin`, `--http1.1|--http2`, `--minutes N`).
- `tools/dev_calendar.py` - creates, changes, deletes and lists `DAVBRIDGE-TEST` events of an explicitly named configured test user (`--config`, `--user`, `--password-stdin`).

### Adding tests

- Prefer unit tests with synthetic fixtures under `tests/fixtures/`.
- Protocol behavior (EWS SOAP, EAS WBXML) is covered by golden files and transcript tests; add a fixture
  that reproduces your case rather than loosening an assertion.
- Fixtures contain **only synthetic events** (for example "Team sync", `alice@example.com`).

## Pull requests

1. Open an issue first for anything larger than a small fix, so we can agree on the approach.
2. Keep the change focused; do not mix refactoring or renaming with functional changes.
3. Make sure tests and linters pass locally.
4. Update the documentation and `CHANGELOG.md` (section "Unreleased") when behavior changes.
5. Fill in the pull request template.

### Developer Certificate of Origin (DCO)

We use the [Developer Certificate of Origin 1.1](https://developercertificate.org/) instead of a
CLA. By adding a `Signed-off-by` line to each commit you certify that you wrote the contribution or
otherwise have the right to submit it under the project's MIT license:

```
Signed-off-by: Your Name <your.email@example.com>
```

Use `git commit -s` to add it automatically. The name and e-mail address in the sign-off become part of the
public history. Contributions without a sign-off cannot be merged.

The text of the DCO 1.1:

> By making a contribution to this project, I certify that:
>
> (a) The contribution was created in whole or in part by me and I have the right to submit it under the
> open source license indicated in the file; or
>
> (b) The contribution is based upon previous work that, to the best of my knowledge, is covered under an
> appropriate open source license and I have the right under that license to submit that work with
> modifications, whether created in whole or in part by me, under the same open source license (unless I am
> permitted to submit under a different license), as indicated in the file; or
>
> (c) The contribution was provided directly to me by some other person who certified (a), (b) or (c) and I
> have not modified it.
>
> (d) I understand and agree that this project and the contribution are public and that a record of the
> contribution (including all personal information I submit with it, including my sign-off) is maintained
> indefinitely and may be redistributed consistent with this project or the open source license(s) involved.

## Reporting bugs and requesting features

Use the issue templates. For bugs, include versions, client, and the output of `davbridge check --json`;
**remove secrets and calendar content from logs before posting**. Security problems must be reported
privately, see [SECURITY.md](SECURITY.md).

There is no support commitment; maintainers answer when they have time.
