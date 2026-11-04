"""Minimal TOML writer for the configuration documents DAVbridge itself produces (strings, booleans, integers, lists, tables)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar(v) for v in value) + "]"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, Mapping) for v in value)


def _emit(table: Mapping[str, Any], path: tuple[str, ...], out: list[str]) -> None:
    scalars = [(k, v) for k, v in table.items() if not isinstance(v, Mapping) and not _is_table_array(v)]
    for key, value in scalars:
        out.append(f"{key} = {_scalar(value)}")
    for key, value in table.items():
        if isinstance(value, Mapping):
            only_tables = bool(value) and all(isinstance(v, Mapping) for v in value.values())
            if not only_tables:
                out.append("")
                out.append(f"[{'.'.join(path + (key,))}]")
            _emit(value, path + (key,), out)
        elif _is_table_array(value):
            for item in value:
                out.append("")
                out.append(f"[[{'.'.join(path + (key,))}]]")
                _emit(item, path + (key,), out)


def dumps(document: Mapping[str, Any], header: str = "") -> str:
    out: list[str] = [f"# {line}" if line else "#" for line in header.splitlines()] if header else []
    if out:
        out.append("")
    _emit(document, (), out)
    return "\n".join(out).replace("\n\n\n", "\n\n") + "\n"
