"""Names <-> (page, token) and helpers to build and read node trees. Pure. Qualified names: ``"AirSync:Sync"``."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping

from .tags_table import CODEPAGES
from .wbxml import Node

PAGES: Final[Mapping[str, int]] = MappingProxyType({name: page for page, (name, _) in CODEPAGES.items()})
_TO_TOKEN: Final[Mapping[int, Mapping[str, int]]] = MappingProxyType({page: MappingProxyType({tag: tok for tok, tag in tab.items()}) for page, (_, tab) in CODEPAGES.items()})


def tag(qname: str) -> tuple[int, int]:
    """``"AirSync:Sync"`` -> ``(0, 0x05)``. KeyError for an unknown page or name."""
    page_name, _, name_ = qname.partition(":")
    page = PAGES[page_name]
    return page, _TO_TOKEN[page][name_]


def name(page: int, token: int) -> str | None:
    entry = CODEPAGES.get(page)
    if entry is None:
        return None
    n = entry[1].get(token)
    return f"{entry[0]}:{n}" if n else None


def display(k: Node) -> str:
    """Name for log and skeleton; unknown tokens as ``Page:0xNN``."""
    return name(k.page, k.token) or f"{k.page}:0x{k.token:02X}"


def node(qname: str, *children: Node | None, text: str | None = None, data: bytes | None = None) -> Node:
    page, token = tag(qname)
    return Node(page, token, text, data, tuple(k for k in children if k is not None))


def leaf(qname: str, value: object | None) -> Node | None:
    """Element with a text value; None for None (for optional fields)."""
    if value is None:
        return None
    return node(qname, text=str(value))


def is_named(k: Node, qname: str) -> bool:
    return (k.page, k.token) == tag(qname)


def find(k: Node, qname: str) -> Node | None:
    target = tag(qname)
    for kind in k.children:
        if (kind.page, kind.token) == target:
            return kind
    return None


def find_all(k: Node, qname: str) -> tuple[Node, ...]:
    target = tag(qname)
    return tuple(kind for kind in k.children if (kind.page, kind.token) == target)


def text_from(k: Node, qname: str) -> str | None:
    kind = find(k, qname)
    if kind is None:
        return None
    return kind.text if kind.text is not None else ""


def path(k: Node, *qnames: str) -> Node | None:
    current: Node | None = k
    for q in qnames:
        if current is None:
            return None
        current = find(current, q)
    return current
