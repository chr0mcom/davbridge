"""Publication hygiene: tracked files must not contain terms from a private, maintainer-only list.

The list is deliberately not part of the repository. Point DAVBRIDGE_PRIVATE_TERMS_FILE at a text file with one
case-insensitive regular expression per line (blank lines and lines starting with # are ignored). Without the
variable these tests are skipped.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXCEPTIONS = {"LICENSE", "NOTICE"}


def private_pattern() -> re.Pattern[str]:
    source = os.environ.get("DAVBRIDGE_PRIVATE_TERMS_FILE")
    if not source:
        pytest.skip("DAVBRIDGE_PRIVATE_TERMS_FILE not set")
    terms = [line.strip() for line in Path(source).read_text(encoding="utf-8").splitlines()]
    terms = [t for t in terms if t and not t.startswith("#")]
    if not terms:
        pytest.skip("private term list is empty")
    return re.compile("|".join(f"(?:{t})" for t in terms), re.IGNORECASE)


def tracked_files() -> list[str]:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("no git checkout")
    output = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [p for p in output.decode().split("\0") if p]


def test_no_private_references_in_tracked_files():
    pattern = private_pattern()
    hit = []
    for rel in tracked_files():
        if rel in EXCEPTIONS or not (ROOT / rel).is_file():
            continue
        raw = (ROOT / rel).read_bytes()
        if b"\0" in raw:
            continue
        for number, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
            if pattern.search(line):
                hit.append(f"{rel}:{number}")
    assert not hit, "forbidden references: " + ", ".join(hit[:20])


def test_no_private_references_in_file_names():
    pattern = private_pattern()
    hit = [rel for rel in tracked_files() if rel not in EXCEPTIONS and pattern.search(rel)]
    assert not hit, "forbidden file names: " + ", ".join(hit)
