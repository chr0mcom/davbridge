"""WBXML codec for Exchange ActiveSync ([MS-ASWBXML]). Pure: no I/O, no asyncio, no mutable globals.

Schema-free: the decoder only knows (page, token); names come from `tags.py`. Iterative (no recursion stack), all limits are
checked before memory grows. Only SWITCH_PAGE, END, STR_I, OPAQUE and tags without attributes are allowed; the string table must be empty.
Exceptions carry only a fixed reason, never user data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, Literal

SWITCH_PAGE: Final = 0x00
END: Final = 0x01
STR_I: Final = 0x03
OPAQUE: Final = 0xC3
HEADER: Final = b"\x03\x01\x6a\x00"
_FORBIDDEN_GLOBAL: Final = frozenset({0x02, 0x04, 0x40, 0x41, 0x42, 0x43, 0x80, 0x81, 0x82, 0x83, 0xC0, 0xC1, 0xC2, 0xC4})
_CONTROL: Final = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")

Reason = Literal["header", "charset", "string-table", "attribute", "literal", "token", "depth", "nodes", "size", "code-page", "truncated", "utf8", "number"]


@dataclass(frozen=True, slots=True)
class Node:
    page: int
    token: int
    text: str | None = None
    data: bytes | None = None
    children: tuple["Node", ...] = ()


@dataclass(frozen=True, slots=True)
class Limits:
    max_bytes: int = 512 * 1024
    max_depth: int = 24
    max_nodes: int = 20_000
    max_text: int = 256 * 1024
    max_page_switch: int = 2_000


class WbxmlError(Exception):
    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason = reason


def _number(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for i in range(5):
        if pos >= len(data):
            raise WbxmlError("truncated")
        b = data[pos]
        pos += 1
        if i == 0 and b == 0x80:
            raise WbxmlError("number")
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            if value > 0xFFFFFFFF:
                raise WbxmlError("number")
            return value, pos
    raise WbxmlError("number")


def _write_number(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise WbxmlError("number")
    parts = [value & 0x7F]
    value >>= 7
    while value:
        parts.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(parts))


@dataclass(slots=True)
class _Open:
    page: int
    token: int
    text: list[str] = field(default_factory=list)
    length: int = 0
    data: bytes | None = None
    children: list[Node] = field(default_factory=list)

    def close(self) -> Node:
        return Node(self.page, self.token, "".join(self.text) if self.text else None, self.data, tuple(self.children))


def decode(data: bytes, limits: Limits = Limits()) -> Node:
    """WBXML -> node tree. Raises WbxmlError for anything that is not clean EAS WBXML."""
    if not data or len(data) > limits.max_bytes:
        raise WbxmlError("size")
    if data[0] not in (0x01, 0x02, 0x03):
        raise WbxmlError("header")
    public_id, pos = _number(data, 1)
    if public_id != 0x01:
        raise WbxmlError("header")
    charset, pos = _number(data, pos)
    if charset != 0x6A:
        raise WbxmlError("charset")
    strtbl, pos = _number(data, pos)
    if strtbl != 0:
        raise WbxmlError("string-table")
    page = 0
    change = 0
    node = 0
    stack: list[_Open] = []
    root: Node | None = None
    n = len(data)
    while pos < n:
        b = data[pos]
        pos += 1
        if b == SWITCH_PAGE:
            if pos >= n:
                raise WbxmlError("truncated")
            page = data[pos]
            pos += 1
            change += 1
            if change > limits.max_page_switch:
                raise WbxmlError("code-page")
            if page > 0x1F:
                raise WbxmlError("token")
            continue
        if b == END:
            if not stack:
                raise WbxmlError("token")
            done = stack.pop().close()
            if stack:
                stack[-1].children.append(done)
                continue
            root = done
            break
        if b == STR_I:
            end = data.find(b"\x00", pos, min(n, pos + limits.max_text + 1))
            if end < 0:
                raise WbxmlError("truncated" if n - pos <= limits.max_text else "size")
            if not stack or stack[-1].children or stack[-1].data is not None:
                raise WbxmlError("token")
            try:
                part = data[pos:end].decode("utf-8")
            except UnicodeDecodeError:
                raise WbxmlError("utf8") from None
            above = stack[-1]
            above.text.append(part)
            above.length += len(part)
            if above.length > limits.max_text:
                raise WbxmlError("size")
            pos = end + 1
            continue
        if b == OPAQUE:
            length, pos = _number(data, pos)
            if length > limits.max_text:
                raise WbxmlError("size")
            if pos + length > n:
                raise WbxmlError("truncated")
            if not stack or stack[-1].children or stack[-1].text or stack[-1].data is not None:
                raise WbxmlError("token")
            stack[-1].data = data[pos : pos + length]
            pos += length
            continue
        if b in _FORBIDDEN_GLOBAL:
            raise WbxmlError("literal")
        if b & 0x80:
            raise WbxmlError("attribute")
        token = b & 0x3F
        if token < 0x05:
            raise WbxmlError("token")
        node += 1
        if node > limits.max_nodes:
            raise WbxmlError("nodes")
        if stack and (stack[-1].text or stack[-1].data is not None):
            raise WbxmlError("token")
        if not stack and root is not None:
            raise WbxmlError("token")
        if b & 0x40:
            if len(stack) >= limits.max_depth:
                raise WbxmlError("depth")
            stack.append(_Open(page, token))
            continue
        empty = Node(page, token)
        if stack:
            stack[-1].children.append(empty)
        else:
            root = empty
            break
    if root is None or stack:
        raise WbxmlError("truncated")
    if pos != n:
        raise WbxmlError("token")
    return root


def clean(text: str) -> str:
    """Inline strings without C0 control characters (except TAB/LF/CR), without surrogates, without the non-characters U+FFFE/U+FFFF."""
    return _CONTROL.sub("", text)


def encode(root: Node) -> bytes:
    """Node tree -> WBXML 1.3, public id 1, UTF-8, without a string table. Iterative."""
    out = bytearray(HEADER)
    page = 0
    work: list[Node | None] = [root]
    while work:
        k = work.pop()
        if k is None:
            out.append(END)
            continue
        if not 0 <= k.page <= 0x1F or not 0x05 <= k.token <= 0x3F:
            raise WbxmlError("token")
        if (k.text is not None or k.data is not None) and k.children:
            raise WbxmlError("token")
        if k.text is not None and k.data is not None:
            raise WbxmlError("token")
        if k.page != page:
            out.extend((SWITCH_PAGE, k.page))
            page = k.page
        text = clean(k.text) if k.text else None
        if not k.children and not text and k.data is None:
            out.append(k.token)
            continue
        out.append(k.token | 0x40)
        if text:
            raw = text.encode("utf-8")
            if len(raw) > Limits().max_text:
                raise WbxmlError("size")
            out.append(STR_I)
            out.extend(raw)
            out.append(0)
        elif k.data is not None:
            out.append(OPAQUE)
            out.extend(_write_number(len(k.data)))
            out.extend(k.data)
        work.append(None)
        work.extend(reversed(k.children))
    return bytes(out)
