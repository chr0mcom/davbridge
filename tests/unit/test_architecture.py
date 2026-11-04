"""Architecture rules and dependency direction (docs/architecture.md), checked by an AST/import scan of the sources in src/davbridge/."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "src" / "davbridge"
FILES = sorted(p for p in APP.rglob("*.py"))
PURE = ["identifiers", "ical", "permissions", "soap", "errors", "protocol_capture"]
OPS = {"sync", "autodiscover", "notifications", "items", "auxiliary", "folders"}


def tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imports(path: Path) -> set[str]:
    names: set[str] = set()
    for n in ast.walk(tree(path)):
        if isinstance(n, ast.Import):
            names |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            names.add(("." * n.level) + (n.module or ""))
            names |= {("." * n.level) + (n.module + "." if n.module else "") + a.name for a in n.names}
    return names


def rel(path: Path) -> str:
    return str(path.relative_to(APP))


def test_app_sources_present():
    assert len(FILES) > 15


@pytest.mark.parametrize("path", FILES, ids=rel)
def test_c2_no_blocking_io_in_the_event_loop(path):
    forbidden = {"requests", "urllib.request", "http.client"}
    found = imports(path)
    assert not (found & forbidden), f"{rel(path)} imports {found & forbidden}"
    for n in ast.walk(tree(path)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "sleep" and isinstance(n.func.value, ast.Name) and n.func.value.id == "time":
            pytest.fail(f"{rel(path)}:{n.lineno} uses time.sleep")


@pytest.mark.parametrize("path", FILES, ids=rel)
def test_sqlite_only_in_the_store(path):
    if path.name == "store.py":
        return
    assert "sqlite3" not in imports(path), f"{rel(path)} opens sqlite3 outside store.py"


@pytest.mark.parametrize("name", PURE)
def test_pure_modules_without_asyncio_and_network(name):
    found = imports(APP / f"{name}.py")
    for forbidden in ("asyncio", "httpx", "sqlite3", "requests", "threading", "socket"):
        assert forbidden not in found, f"{name}.py imports {forbidden}"
    assert not any(isinstance(n, ast.Global) for n in ast.walk(tree(APP / f"{name}.py"))), f"{name}.py uses global"
    assert not any(isinstance(n, ast.AsyncFunctionDef) for n in ast.walk(tree(APP / f"{name}.py"))) or name == "protocol_capture", f"{name}.py contains async functions"


def test_httpx_only_in_caldav_and_main():
    allowed = {"caldav.py", "main.py", "cli.py", "check.py"}
    for path in FILES:
        if any(i == "httpx" or i.startswith("httpx.") for i in imports(path)):
            assert path.name in allowed, f"{rel(path)} imports httpx"


def test_no_basehttpmiddleware_and_no_compression():
    forbidden = {"BaseHTTPMiddleware", "GZipMiddleware"}
    for path in FILES:
        for n in ast.walk(tree(path)):
            names = set()
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
            elif isinstance(n, ast.alias):
                names.add(n.name.split(".")[-1])
            assert not (names & forbidden), f"{rel(path)} uses {names & forbidden}"


COMPOSE = ROOT / "examples" / "compose" / "docker-compose.yml"


def test_examples_without_compression_and_chain():
    for path in [COMPOSE, *sorted((ROOT / "examples" / "reverse-proxy").glob("*"))]:
        text = path.read_text(encoding="utf-8")
        for forbidden in ("compression@file", "chain@file", "chain-spa@file", "BaseHTTPMiddleware", "compress:", "gzip on;"):
            assert forbidden not in text, f"{path.name}: {forbidden}"


def test_no_module_imports_main():
    for path in FILES:
        if path.name == "main.py":
            continue
        for i in imports(path):
            assert not (i.endswith(".main") or i == ".main" or i == "davbridge.main"), f"{rel(path)} imports main"


@pytest.mark.parametrize("path", sorted((APP / "operations").glob("*.py")), ids=rel)
def test_ops_do_not_import_each_other(path):
    if path.name == "__init__.py":
        return
    for i in imports(path):
        parts = i.lstrip(".").split(".")
        if i.startswith(".") and not i.startswith(".."):
            assert parts[0] not in OPS, f"{rel(path)} imports sibling module {i}"


def test_ops_init_imports_only_in_import_all_siblings():
    source = (APP / "operations" / "__init__.py").read_text(encoding="utf-8")
    header = source.split("def import_all")[0]
    for n in OPS:
        assert f"from . import {n}" not in header and f"from .{n} import" not in header


def test_only_caldav_py_speaks_http_with_nextcloud():
    for path in FILES:
        if path.name in ("caldav.py", "main.py", "cli.py"):
            continue
        text = path.read_text(encoding="utf-8")
        assert "remote.php" not in text, f"{rel(path)} knows CalDAV server URLs"


def test_no_secret_formatting_in_exceptions():
    """No str(e)/repr(e)/{e} on the logging path. Rough check: log.* is never called with str(...)/repr(...) of exceptions."""
    for path in FILES:
        for n in ast.walk(tree(path)):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("event", "error") and "log" in ast.dump(n.func):
                for a in ast.walk(n):
                    if isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and a.func.id in ("str", "repr"):
                        pytest.fail(f"{rel(path)}:{n.lineno} formats an exception/value in a log call")


EAS = APP / "eas"
EAS_PURE_MANDATORY = ["wbxml", "tags", "tags_table", "timezone", "calendar", "recurrence"]
EAS_PURE_OPTIONAL = ["request", "skeleton"]
EAS_PURE = EAS_PURE_MANDATORY + [n for n in EAS_PURE_OPTIONAL if (EAS / f"{n}.py").is_file()]
EWS_MODULE = {"operations", "soap", "protocol_capture", "main"}


@pytest.mark.parametrize("name", EAS_PURE_MANDATORY)
def test_eas_pure_modules_exist(name):
    assert (EAS / f"{name}.py").is_file()


@pytest.mark.parametrize("name", EAS_PURE)
def test_eas_pure_modules_without_asyncio_network_and_global(name):
    path = EAS / f"{name}.py"
    found = imports(path)
    for forbidden in ("asyncio", "httpx", "sqlite3", "requests", "threading", "socket"):
        assert forbidden not in found, f"eas/{name}.py imports {forbidden}"
    assert not any(isinstance(n, ast.Global) for n in ast.walk(tree(path))), f"eas/{name}.py uses global"
    assert not any(isinstance(n, ast.AsyncFunctionDef) for n in ast.walk(tree(path))), f"eas/{name}.py contains async functions"


def _ews_imports_from(source: str, level: int) -> set[str]:
    """Imports pointing to operations, soap, protocol_capture or main of the package (relative from the matching level, or absolute)."""
    names: set[str] = set()
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.Import):
            names |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            names.add(("." * n.level) + (n.module or ""))
            names |= {("." * n.level) + (n.module + "." if n.module else "") + a.name for a in n.names}
    hit = set()
    for i in names:
        points = len(i) - len(i.lstrip("."))
        parts = i.lstrip(".").split(".")
        if points == level and parts[0] in EWS_MODULE:
            hit.add(i)
        elif points == 0 and parts[0] == "davbridge" and len(parts) > 1 and parts[1] in EWS_MODULE:
            hit.add(i)
    return hit


@pytest.mark.parametrize("path", sorted(EAS.rglob("*.py")), ids=rel)
def test_eas_imports_neither_operations_nor_soap_capture_or_main(path):
    found = _ews_imports_from(path.read_text(encoding="utf-8"), len(path.relative_to(APP).parts))
    assert not found, f"{rel(path)} imports {found}"


def test_eas_import_check_detects_forbidden_imports():
    source = "from .. import soap\nfrom ..operations import items\nimport davbridge.main\nfrom ..ical import lies\nfrom . import wbxml\nfrom .protocol_capture import x\n"
    assert _ews_imports_from(source, 2) == {"..soap", "..operations", "..operations.items", "davbridge.main"}
    assert _ews_imports_from("from ... import main\nfrom ...soap import x\n", 3) == {"...main", "...soap", "...soap.x"}
