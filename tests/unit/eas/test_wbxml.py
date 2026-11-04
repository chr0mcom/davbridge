"""Codec tests. Specification vector: MS-ASCMD FolderSync request with SyncKey 0 (page 7: FolderSync 0x16, SyncKey 0x12)."""

import random
import time

import pytest

from davbridge.eas import tags, wbxml
from davbridge.eas.wbxml import Limits, Node, WbxmlError

FOLDERSYNC_0 = bytes.fromhex("03016a00000756520330000101")
K = bytes.fromhex("03016a00")


def test_foldersync_vector():
    w = wbxml.decode(FOLDERSYNC_0)
    assert tags.display(w) == "FolderHierarchy:FolderSync"
    assert tags.text_from(w, "FolderHierarchy:SyncKey") == "0"
    assert wbxml.encode(w) == FOLDERSYNC_0


def tree():
    return tags.node(
        "AirSync:Sync",
        tags.node(
            "AirSync:Collections",
            tags.node(
                "AirSync:Collection",
                tags.node("AirSync:SyncKey", text="1"),
                tags.node("AirSync:GetChanges"),
                tags.node(
                    "AirSync:Commands",
                    tags.node(
                        "AirSync:Add",
                        tags.node(
                            "AirSync:ApplicationData",
                            tags.node("Calendar:Subject", text="Dentist – Café 🦷"),
                            tags.node("Calendar:Timezone", data=b"\x00\x01\xff"),
                            tags.node("AirSyncBase:Body", tags.node("AirSyncBase:Type", text="1")),
                        ),
                    ),
                ),
            ),
        ),
    )


def test_roundtrip_with_page_switch_empty_and_opaque():
    raw = wbxml.encode(tree())
    assert wbxml.decode(raw) == tree()
    assert wbxml.encode(wbxml.decode(raw)) == raw


def _random(rnd, depth):
    children = tuple(_random(rnd, depth + 1) for _ in range(rnd.randint(0, 3))) if depth < 6 and rnd.random() < 0.6 else ()
    page = rnd.choice([0, 4, 7, 13, 17, 18])
    token = rnd.randint(5, 0x3F)
    if children:
        return Node(page, token, children=children)
    kind = rnd.random()
    if kind < 0.3:
        return Node(page, token)
    if kind < 0.6:
        return Node(page, token, data=bytes(rnd.randrange(256) for _ in range(rnd.randint(0, 20))))
    return Node(page, token, text="".join(chr(rnd.choice([0x41, 0xE4, 0x1F600, 0x20AC, 0x9])) for _ in range(rnd.randint(1, 8))))


def test_roundtrip_random():
    rnd = random.Random(4711)
    for _ in range(5000):
        w = _random(rnd, 0)
        assert wbxml.decode(wbxml.encode(w)) == w


def test_unknown_token_stays_structurally_readable():
    w = wbxml.decode(K + bytes([0x45, 0x3E, 0x01]))
    assert tags.display(w.children[0]) == "0:0x3E"


@pytest.mark.parametrize(
    "raw,reason",
    [
        (b"", "size"),
        (bytes.fromhex("04016a00"), "header"),
        (bytes.fromhex("03006a00"), "header"),
        (bytes.fromhex("03016a0100"), "string-table"),
        (bytes.fromhex("0301040000"), "charset"),
        (K + b"\xc5", "attribute"),
        (K + b"\x45\x02", "literal"),
        (K + b"\x45\x43", "literal"),
        (K + b"\x44", "token"),
        (K + b"\x45\x83\x00\x01", "literal"),
        (K + b"\x45\x03AB", "truncated"),
        (K + b"\x45\x03\xff\x00\x01", "utf8"),
        (K + b"\x45\x03a\x00\x46\x01\x01", "token"),
        (K + b"\x45\xc3\x8f\xff\xff\xff\x7f", "size"),
        (K + b"\x45\xc3\x9f\xff\xff\xff\x7f", "number"),
        (K + b"\x45\xc3\x05ab", "truncated"),
        (K + b"\x45", "truncated"),
        (K + b"\x45\x01\x01", "token"),
        (K + b"\x05\x05", "token"),
        (K + b"\x00\x63", "token"),
        (K + b"\x45\xc3\x80\x01", "number"),
    ],
)
def test_invalid(raw, reason):
    with pytest.raises(WbxmlError) as e:
        wbxml.decode(raw)
    assert e.value.reason == reason


def test_bombs_fast_and_bounded():
    for raw in (K + b"\x45" * 10_000, K + b"\x45" + b"\x05" * 1_000_000 + b"\x01", K + b"\x00\x01" * 3000 + b"\x05", K + b"\x45\x03" + b"a" * 400_000):
        start = time.monotonic()
        with pytest.raises(WbxmlError):
            wbxml.decode(raw, Limits(max_bytes=4 << 20))
        assert time.monotonic() - start < 1.0


def test_limits_individually():
    with pytest.raises(WbxmlError) as e:
        wbxml.decode(K + b"\x45" * 25 + b"\x01" * 25)
    assert e.value.reason == "depth"
    with pytest.raises(WbxmlError) as e:
        wbxml.decode(K + b"\x45" + b"\x05" * 20_000 + b"\x01")
    assert e.value.reason == "nodes"
    with pytest.raises(WbxmlError) as e:
        wbxml.decode(b"\x03" * (512 * 1024 + 1))
    assert e.value.reason == "size"


def test_fuzz_only_wbxml_error():
    rnd = random.Random(42)
    valid = wbxml.encode(tree())
    for _ in range(20_000):
        b = bytearray(valid)
        for _ in range(rnd.randint(1, 4)):
            b[rnd.randrange(len(b))] = rnd.randrange(256)
        if rnd.random() < 0.2:
            del b[rnd.randrange(len(b)) :]
        try:
            wbxml.decode(bytes(b))
        except WbxmlError:
            pass


def test_encode_sanitizes_and_checks():
    raw = wbxml.encode(Node(0, 0x0B, text="a\x00b\x07c\td"))
    assert wbxml.decode(raw).text == "abc\td"
    with pytest.raises(WbxmlError):
        wbxml.encode(Node(0, 0x04))
    with pytest.raises(WbxmlError):
        wbxml.encode(Node(0, 0x05, text="x", children=(Node(0, 0x06),)))
    with pytest.raises(KeyError):
        tags.node("AirSync:DoesNotExist")


def test_number_limits():
    assert wbxml._write_number(0) == b"\x00"
    assert wbxml._write_number(0x80) == b"\x81\x00"
    assert wbxml._number(wbxml._write_number(0xFFFFFFFF), 0) == (0xFFFFFFFF, 5)
