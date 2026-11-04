"""Packaging contract: version, entry point, dev extra, package list and shipped data."""

import hashlib
import subprocess
import tomllib
from pathlib import Path

import davbridge

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_version_is_0_1_0_and_uniform():
    assert PYPROJECT["project"]["version"] == "0.1.0" == davbridge.__version__


def test_console_command_and_build_backend():
    assert PYPROJECT["project"]["scripts"] == {"davbridge": "davbridge.cli:main"}
    assert PYPROJECT["build-system"]["requires"] == ["setuptools>=77"]


def test_all_licence_texts_are_declared_for_the_wheel():
    assert PYPROJECT["project"]["license-files"] == ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"]
    for name in PYPROJECT["project"]["license-files"]:
        assert (ROOT / name).is_file()


def test_copyright_line_is_identical_in_licence_and_notice():
    line = next(x for x in (ROOT / "LICENSE").read_text().splitlines() if x.startswith("Copyright (c)"))
    assert (ROOT / "NOTICE").read_text().splitlines()[1] == line


def test_dev_extra_for_the_ci():
    dev = {r.split(">")[0].split("=")[0].split("<")[0] for r in PYPROJECT["project"]["optional-dependencies"]["dev"]}
    assert {"pytest", "pytest-asyncio", "pytest-cov", "ruff"} <= dev


def test_package_list_is_complete():
    src = ROOT / "src"
    found = {".".join(p.parent.relative_to(src).parts) for p in (src / "davbridge").rglob("__init__.py")}
    configured = set(PYPROJECT["tool"]["setuptools"]["packages"])
    assert configured == found | {"davbridge.data"}


def test_timezone_file_is_delivered_as_package_data():
    setuptools = PYPROJECT["tool"]["setuptools"]
    assert setuptools["package-dir"]["davbridge.data"] == "data"
    assert setuptools["package-data"]["davbridge.data"] == ["windowsZones.xml"]
    file = ROOT / "data" / "windowsZones.xml"
    assert file.is_file()
    assert hashlib.sha256(file.read_bytes()).hexdigest() in (ROOT / "data" / "README.md").read_text()


def test_timezones_finds_the_file_even_without_repository_layout(tmp_path, monkeypatch):
    """Installed layout: <site-packages>/davbridge/data/windowsZones.xml next to the module."""
    import shutil

    from davbridge import timezones

    package = tmp_path / "site" / "davbridge"
    (package / "data").mkdir(parents=True)
    shutil.copy(ROOT / "data" / "windowsZones.xml", package / "data" / "windowsZones.xml")
    shutil.copy(Path(timezones.__file__), package / "timezones.py")
    monkeypatch.setattr(timezones, "__file__", str(package / "timezones.py"))
    assert timezones._data_path() == (package / "data" / "windowsZones.xml").resolve()


DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_dockerfile_stages_share_one_digest_pinned_multi_arch_base():
    import re

    bases = re.findall(r"^FROM (\S+) AS (\w+)$", DOCKERFILE, re.M)
    assert [name for _, name in bases] == ["builder", "runtime"]
    assert bases[0][0] == bases[1][0] and re.fullmatch(r"python:3\.12-slim-[a-z]+@sha256:[0-9a-f]{64}", bases[0][0])


def test_dockerfile_hardening_contract():
    assert "USER 65532:65532" in DOCKERFILE
    assert 'HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 CMD ["davbridge", "healthcheck"]' in DOCKERFILE
    assert 'CMD ["davbridge", "serve"]' in DOCKERFILE
    assert "--require-hashes" in DOCKERFILE and "VOLUME" in DOCKERFILE
    for label in ("title", "description", "source", "licenses", "version", "revision", "created"):
        assert f"org.opencontainers.image.{label}=" in DOCKERFILE
    assert 'org.opencontainers.image.source="https://github.com/chr0mcom/davbridge"' in DOCKERFILE and 'org.opencontainers.image.licenses="MIT"' in DOCKERFILE
    assert "ARG VERSION" in DOCKERFILE and "ARG REVISION" in DOCKERFILE and "ARG BUILD_DATE" in DOCKERFILE
    assert "--workers" not in DOCKERFILE


def test_release_workflow_builds_what_the_dockerfile_declares():
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "platforms: linux/amd64,linux/arm64" in release and "ghcr.io/chr0mcom/davbridge" in release
    for arg in ("VERSION=", "REVISION=", "BUILD_DATE="):
        assert arg in release
    assert "docker/setup-buildx-action" in release and "docker/setup-qemu-action" in release
    assert "sbom: true" in release and "provenance: mode=max" in release


def test_compose_example_matches_the_image():
    compose = (ROOT / "examples" / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"davbridge", "healthcheck"' in compose and 'user: "65532:65532"' in compose and "read_only: true" in compose


def lock_text(name: str) -> str:
    return (ROOT / "requirements" / name).read_text(encoding="utf-8")


def pins(text: str) -> dict[str, str]:
    import re

    return {m.group(1).lower(): m.group(2) for m in re.finditer(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", text, re.M)}


def test_every_lock_pin_has_hashes_and_the_old_unhashed_files_are_gone():
    import re

    for name in ("runtime.txt", "build.txt", "dev.txt"):
        text = lock_text(name)
        blocks = re.split(r"\n(?=[A-Za-z0-9_.\-]+==)", text)[1:]
        assert blocks, name
        for block in blocks:
            assert "--hash=sha256:" in block, f"{name}: {block.splitlines()[0]} has no hash"
    assert not (ROOT / "requirements.lock").exists() and not (ROOT / "requirements.in").exists()


def test_build_lock_contains_setuptools_and_wheel_and_dev_lock_contains_the_test_tools():
    assert {"setuptools", "wheel"} <= set(pins(lock_text("build.txt")))
    assert {"pytest", "pytest-asyncio", "pytest-cov", "ruff"} <= set(pins(lock_text("dev.txt")))


def test_dev_lock_uses_the_runtime_versions():
    runtime, dev = pins(lock_text("runtime.txt")), pins(lock_text("dev.txt"))
    assert runtime and all(dev[name] == version for name, version in runtime.items())


def test_lock_inputs_match_pyproject():
    import re

    def names(lines):
        return {re.split(r"[<>=!~ ]", x, 1)[0].lower() for x in lines}

    def requirements(name):
        return [x.strip() for x in lock_text(name).splitlines() if x.strip() and not x.startswith(("#", "-"))]

    assert sorted(requirements("runtime.in")) == sorted(PYPROJECT["project"]["dependencies"])
    assert requirements("build.in") == [r for r in PYPROJECT["build-system"]["requires"]] + ["wheel"]
    assert names(requirements("dev.in")) == names(PYPROJECT["project"]["optional-dependencies"]["dev"])


def test_nothing_installs_unhashed_tooling_or_upgrades_pip():
    sources = {"Dockerfile": DOCKERFILE}
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        sources[path.name] = path.read_text(encoding="utf-8")
    for name, text in sources.items():
        assert "--upgrade pip" not in text and "-U pip" not in text, name
        assert 'pip install -e ".[dev]"' not in text and "requirements.lock" not in text, name
        for line in text.splitlines():
            if "pip wheel" in line and "#" not in line.split("pip wheel")[0]:
                assert "--no-build-isolation" in line or "\\" in line, f"{name}: {line.strip()}"
    assert "--no-build-isolation" in DOCKERFILE and "requirements/build.txt" in DOCKERFILE and "requirements/runtime.txt" in DOCKERFILE
    ci = sources["ci.yml"]
    assert "requirements/dev.txt" in ci and "requirements/build.txt" in ci and "requirements/runtime.txt" in ci
    assert "requirements/build.txt" in sources["release.yml"]


def test_dependabot_watches_the_requirements_directory():
    text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert 'package-ecosystem: "pip"\n    directory: "/requirements"' in text


RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def test_release_pushes_only_a_staging_tag_before_the_scan_and_promotes_afterwards():
    assert "type=sha,format=long,prefix=sha-" in RELEASE and "format=short" not in RELEASE
    build, scan, promote = (RELEASE.index(marker) for marker in ("docker/build-push-action", "aquasecurity/trivy-action", "docker buildx imagetools create"))
    assert build < scan < promote
    scans = RELEASE[scan - 200 : promote]
    assert scans.count("aquasecurity/trivy-action") == 2 and "TRIVY_PLATFORM: linux/amd64" in scans and "TRIVY_PLATFORM: linux/arm64" in scans
    assert scans.count("TRIVY_IMAGE_SRC: remote") == 2 and scans.count('exit-code: "1"') == 2
    before_promotion = RELEASE[:promote]
    for moving in ("type=semver", "type=raw,value=latest", "{{version}}", "{{major}}"):
        assert moving not in before_promotion
    assert '"$IMAGE@$DIGEST"' in RELEASE[promote : promote + 200]
    assert "image-ref: ${{ env.IMAGE }}@${{ steps.build.outputs.digest }}" in RELEASE


def _staging_check_script() -> str:
    block = RELEASE.split("- name: Refuse to overwrite an existing staging tag", 1)[1].split("run: |\n", 1)[1]
    lines = []
    for line in block.splitlines():
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line[10:])
    return "\n".join(lines)


SHA = "0123456789abcdef0123456789abcdef01234567"


def _run_staging_check(tmp_path, docker_behaviour: str, *, tags: str | None = None, sha: str = SHA) -> subprocess.CompletedProcess:
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "docker").write_text(f"#!/bin/bash\n{docker_behaviour}\n")
    (stub / "docker").chmod(0o755)
    image = "ghcr.io/chr0mcom/davbridge"
    env = {"PATH": f"{stub}:/usr/bin:/bin", "IMAGE": image, "SHA": sha, "TAGS": tags if tags is not None else f"{image}:sha-{sha}"}
    return subprocess.run(["bash", "-c", _staging_check_script()], env=env, capture_output=True, text=True, cwd=tmp_path)


def test_the_staging_tag_is_the_full_commit_sha_and_is_never_overwritten(tmp_path):
    assert "sha-${{ github.sha }}" not in RELEASE and "type=sha,format=long" in RELEASE
    assert _run_staging_check(tmp_path, 'echo "ERROR: ghcr.io/x:y: not found" >&2; exit 1').returncode == 0
    exists = _run_staging_check(tmp_path, 'echo "Name: x"; exit 0')
    assert exists.returncode == 1 and "already exists" in exists.stdout
    unclear = _run_staging_check(tmp_path, 'echo "dial tcp: i/o timeout" >&2; exit 1')
    assert unclear.returncode == 1 and "Cannot tell" in unclear.stdout
    short = _run_staging_check(tmp_path, "exit 1", tags=f"ghcr.io/chr0mcom/davbridge:sha-{SHA[:7]}")
    assert short.returncode == 1 and "Unexpected staging tag" in short.stdout
    check = RELEASE.index("Refuse to overwrite an existing staging tag")
    assert check < RELEASE.index("docker/build-push-action")


def test_the_version_label_is_set_from_the_verified_version_and_asserted_in_the_smoke_test():
    assert "org.opencontainers.image.version=${{ needs.verify.outputs.version }}" in RELEASE
    smoke = RELEASE.split("- name: Smoke test both platforms by digest", 1)[1].split("- name: Vulnerability scan", 1)[0]
    assert "VERSION: ${{ needs.verify.outputs.version }}" in smoke
    assert 'index .Config.Labels "org.opencontainers.image.version"' in smoke and '[ "$LABEL_VERSION" = "$VERSION" ]' in smoke
    assert "exit 1" in smoke.split('[ "$LABEL_VERSION" = "$VERSION" ]', 1)[1].splitlines()[0]
    assert "version: ${{ steps.v.outputs.version }}" in RELEASE.split("  ci:\n", 1)[0]


def test_only_the_digest_is_called_immutable():
    assert "immutable tag" not in RELEASE.lower() and "sha-<short>" not in RELEASE
    for name in ("CHANGELOG.md", "CONTRIBUTING.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "immutable `sha-" not in text and "immutable tag" not in text and "sha-<short>" not in text, name


def test_release_makes_no_signature_claim_and_attaches_the_sboms():
    lowered = RELEASE.lower()
    assert "git verify-tag" not in lowered and "git tag -s" not in lowered and "signed tag" not in lowered and "todo" not in lowered
    assert "pushed by the maintainer" in RELEASE
    assert "imagetools inspect" in RELEASE and ".SBOM" in RELEASE and "dist/*.spdx.json" in RELEASE


def test_ci_checks_the_private_term_list_when_the_secret_exists():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "secrets.DAVBRIDGE_PRIVATE_TERMS" in ci and "$RUNNER_TEMP/terms.txt" in ci and "DAVBRIDGE_PRIVATE_TERMS_FILE" in ci and "tests/unit/test_hygiene.py" in ci
    assert "secrets: inherit" in RELEASE


def _hygiene_step_script() -> str:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    block = ci.split("- name: Publication hygiene (maintainer-only term list)", 1)[1].split("run: |\n", 1)[1]
    lines = []
    for line in block.splitlines():
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line[10:])
    return "\n".join(lines)


def _run_hygiene_step(tmp_path, terms: str, require: str) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "RUNNER_TEMP": str(tmp_path), "PRIVATE_TERMS": terms, "REQUIRE_TERMS": require}
    return subprocess.run(["bash", "-c", _hygiene_step_script()], env=env, capture_output=True, text=True, cwd=tmp_path)


def test_the_hygiene_gate_fails_closed_for_releases_and_skips_only_where_the_secret_is_unavailable(tmp_path):
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "require_terms:" in ci and "type: boolean" in ci and "default: false" in ci
    assert "REQUIRE_TERMS: ${{ inputs.require_terms && 'true' || 'false' }}" in ci
    refused = _run_hygiene_step(tmp_path, "", "true")
    assert refused.returncode == 1 and "::error::" in refused.stdout and "DAVBRIDGE_PRIVATE_TERMS" in refused.stdout
    skipped = _run_hygiene_step(tmp_path, "", "false")
    assert skipped.returncode == 0 and "::notice::" in skipped.stdout and "skipped" in skipped.stdout
    assert not (tmp_path / "terms.txt").exists()


def test_the_release_workflow_requires_the_private_term_gate():
    ci_job = RELEASE.split("  ci:\n", 1)[1].split("\n  publish:", 1)[0]
    assert "uses: ./.github/workflows/ci.yml" in ci_job and "require_terms: true" in ci_job and "secrets: inherit" in ci_job


def test_the_weekly_integration_run_is_enabled_and_images_in_workflows_are_pinned_by_digest():
    import re

    integration = (ROOT / ".github" / "workflows" / "integration.yml").read_text(encoding="utf-8")
    assert re.search(r"^  schedule:\n    - cron: ", integration, re.M)
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for image in ("nginx:stable", "caddy:2.11.8", "traefik:v3"):
        assert re.search(re.escape(image) + r"@sha256:[0-9a-f]{64}", ci), image
    assert "nextcloud:34.0.5-apache@sha256:" in integration
    assert "TODO" not in ci and "TODO" not in integration


def test_compose_example_joins_the_proxy_network_and_limits_cpu_and_memory():
    compose = (ROOT / "examples" / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "networks: [proxy]" in compose and "external: true" in compose and "name: proxy" in compose
    assert "cpus:" in compose and "mem_limit:" in compose and "pids_limit:" in compose and "ports:" not in compose


def test_installation_guide_explains_how_to_find_the_proxy_address():
    guide = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    assert "docker network inspect" in guide and "trusted_proxies" in guide and "chmod 600 davbridge.toml" not in guide
    assert "/32" in guide and "narrow" in guide


def test_reverse_proxy_guide_does_not_promise_a_caddy_rate_limit():
    text = (ROOT / "docs" / "reverse-proxy.md").read_text(encoding="utf-8")
    assert "all three examples use this" not in text and "Core Caddy has no rate-limit directive" in text


def test_contributing_lists_the_repository_prerequisites_including_every_label_in_use():
    import re

    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    section = text.split("**Before the first release / repository prerequisites**", 1)[1].split("### Test tools", 1)[0]
    used: set[str] = set()
    for path in [*(ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml"), ROOT / ".github" / "dependabot.yml"]:
        for match in re.finditer(r"^\s*labels:\s*\[(.*?)\]", path.read_text(encoding="utf-8"), re.M):
            used |= set(re.findall(r'"([^"]+)"', match.group(1)))
    assert {"bug", "needs-triage", "dependencies"} <= used
    for label in used:
        assert f"`{label}`" in section, label
    lowered = section.lower()
    assert "private vulnerability reporting" in lowered and "DAVBRIDGE_PRIVATE_TERMS" in section and "require_terms" in section
    assert "public" in lowered and "ghcr.io/chr0mcom/davbridge" in section and "cve gate" in lowered


def test_security_policy_says_that_tags_are_not_verified_and_names_the_verification_anchors():
    text = " ".join((ROOT / "SECURITY.md").read_text(encoding="utf-8").split())
    assert "not cryptographically verified yet" in text
    for anchor in ("image digest", "SHA256SUMS", "SBOM", "provenance attestation"):
        assert anchor in text, anchor
