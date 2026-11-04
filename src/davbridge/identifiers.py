"""Signed identifiers. No I/O. Every identifier binds the account into the MAC."""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
from dataclasses import dataclass

VERSION = 1
MAC_LENGTH = 16
CONTEXT = b"davbridge\x00"

T_FOLDER = b"O"
T_ELEMENT = b"E"
T_CHANGE = b"C"
T_FOLDER_CHANGE = b"F"
T_SYNC = b"S"
T_HIERARCHY = b"H"
T_SUBSCRIPTION = b"A"
T_WATERMARK = b"W"
T_EAS_SYNC = b"K"
T_EAS_HIERARCHY = b"J"


class InvalidId(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ElementRef:
    cal: str
    href: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode64(value: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise InvalidId
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError):
        raise InvalidId from None


def _bytes(field: str | bytes) -> bytes:
    return field.encode("utf-8") if isinstance(field, str) else field


class IdService:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("key must be 32 bytes long")
        self._key = key

    def _mac(self, type: bytes, account: str, fields: bytes) -> bytes:
        return hmac.new(self._key, CONTEXT + type + _bytes(account) + b"\x00" + fields, hashlib.sha256).digest()[:MAC_LENGTH]

    def _pack(self, type: bytes, account: str, *fields: str | bytes) -> str:
        parts = bytearray()
        for field in fields:
            raw = _bytes(field)
            if len(raw) > 255:
                raise ValueError("field too long")
            parts += struct.pack(">H", len(raw)) + raw
        payload = bytes([VERSION]) + type + bytes(parts)
        return _b64(payload + self._mac(type, account, bytes(parts)))

    def _unpack(self, type: bytes, account: str, value: str, count: int) -> list[bytes]:
        raw = _decode64(value)
        if len(raw) < 2 + MAC_LENGTH or raw[0] != VERSION or raw[1:2] != type:
            raise InvalidId
        parts, mac = raw[2:-MAC_LENGTH], raw[-MAC_LENGTH:]
        if not hmac.compare_digest(mac, self._mac(type, account, parts)):
            raise InvalidId
        fields: list[bytes] = []
        pos = 0
        while pos < len(parts):
            if pos + 2 > len(parts):
                raise InvalidId
            (length,) = struct.unpack(">H", parts[pos : pos + 2])
            pos += 2
            if pos + length > len(parts):
                raise InvalidId
            fields.append(parts[pos : pos + length])
            pos += length
        if len(fields) != count:
            raise InvalidId
        return fields

    def _text(self, raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidId from None

    def folder_id(self, account: str, folder: str) -> str:
        return self._pack(T_FOLDER, account, folder)

    def read_folder_id(self, account: str, value: str) -> str:
        return self._text(self._unpack(T_FOLDER, account, value, 1)[0])

    def element_id(self, account: str, ref: ElementRef) -> str:
        return self._pack(T_ELEMENT, account, ref.cal, ref.href)

    def read_element_id(self, account: str, value: str) -> ElementRef:
        cal, href = self._unpack(T_ELEMENT, account, value, 2)
        return ElementRef(self._text(cal), self._text(href))

    def change_key(self, account: str, ref: ElementRef, etag: str) -> str:
        return self._pack(T_CHANGE, account, ref.cal, ref.href, etag)

    def read_change_key(self, account: str, ref: ElementRef, value: str) -> str:
        cal, href, etag = self._unpack(T_CHANGE, account, value, 3)
        if self._text(cal) != ref.cal or self._text(href) != ref.href:
            raise InvalidId
        return self._text(etag)

    def folder_change_key(self, account: str, folder: str, fingerprint: bytes) -> str:
        return self._pack(T_FOLDER_CHANGE, account, folder, fingerprint)

    def sync_state(self, account: str, folder: str, state_id: bytes) -> str:
        return self._pack(T_SYNC, account, folder, state_id)

    def read_sync_state(self, account: str, folder: str, value: str) -> bytes:
        read_back, state = self._unpack(T_SYNC, account, value, 2)
        if self._text(read_back) != folder:
            raise InvalidId
        return state

    def hierarchy_state(self, account: str, fingerprint: bytes) -> str:
        return self._pack(T_HIERARCHY, account, fingerprint)

    def read_hierarchy_state(self, account: str, value: str) -> bytes:
        return self._unpack(T_HIERARCHY, account, value, 1)[0]

    def subscription_id(self, account: str, epoch: bytes, subscription: bytes) -> str:
        return self._pack(T_SUBSCRIPTION, account, epoch, subscription)

    def read_subscription_id(self, account: str, value: str) -> tuple[bytes, bytes]:
        epoch, subscription = self._unpack(T_SUBSCRIPTION, account, value, 2)
        return epoch, subscription

    def watermark(self, account: str, subscription: bytes, sequence: int) -> str:
        return self._pack(T_WATERMARK, account, subscription, struct.pack(">Q", sequence))

    def read_watermark(self, account: str, subscription: bytes, value: str) -> int:
        read_back, sequence = self._unpack(T_WATERMARK, account, value, 2)
        if read_back != subscription or len(sequence) != 8:
            raise InvalidId
        return struct.unpack(">Q", sequence)[0]

    def element_hash(self, account: str, ref: ElementRef) -> str:
        raw = hmac.new(self._key, b"hash\x00" + _bytes(account) + b"\x00" + _bytes(ref.cal) + b"\x00" + _bytes(ref.href), hashlib.sha256)
        return raw.hexdigest()[:12]

    def dedup_hash(self, account: str, cal: str, canonical: bytes) -> bytes:
        """Keyed fingerprint for CreateItem without a UID: no content hash in SQLite that could be checked with a dictionary."""
        return hmac.new(self._key, b"dedup\x00" + _bytes(account) + b"\x00" + _bytes(cal) + b"\x00" + canonical, hashlib.sha256).digest()

    def calendar_key(self, account: str, path: str) -> str:
        """Stable public id of a discovered calendar: derived from the canonical collection path, never from the display name."""
        raw = hmac.new(self._key, b"calendar\x00" + _bytes(account) + b"\x00" + _bytes(path), hashlib.sha256).hexdigest()
        return "c-" + raw[:24]

    def secret_hash(self, *parts: str) -> bytes:
        """HMAC over credentials for the positive/negative cache; the password itself is never stored."""
        return hmac.new(self._key, b"credentials\x00" + b"\x00".join(_bytes(t) for t in parts), hashlib.sha256).digest()

    def _compact(self, type: bytes, account: str, binding: bytes, use: bytes) -> str:
        """EAS key of at most 64 characters: device/folder only enter the MAC, the payload carries only the value."""
        return _b64(bytes([VERSION]) + type + use + self._mac(type, account, binding + b"\x00" + use))

    def _read_compact(self, type: bytes, account: str, binding: bytes, value: str, length: int) -> bytes:
        if not isinstance(value, str) or len(value) > 64:
            raise InvalidId
        raw = _decode64(value)
        if len(raw) != 2 + length + MAC_LENGTH or raw[0] != VERSION or raw[1:2] != type:
            raise InvalidId
        use, mac = raw[2:-MAC_LENGTH], raw[-MAC_LENGTH:]
        if not hmac.compare_digest(mac, self._mac(type, account, binding + b"\x00" + use)):
            raise InvalidId
        return use

    def eas_sync_key(self, account: str, device: str, folder: str, counter: int) -> str:
        return self._compact(T_EAS_SYNC, account, _bytes(device) + b"\x00" + _bytes(folder), struct.pack(">Q", counter))

    def read_eas_sync_key(self, account: str, device: str, folder: str, value: str) -> int:
        use = self._read_compact(T_EAS_SYNC, account, _bytes(device) + b"\x00" + _bytes(folder), value, 8)
        return struct.unpack(">Q", use)[0]

    def eas_hierarchy_key(self, account: str, device: str, fingerprint: bytes) -> str:
        return self._compact(T_EAS_HIERARCHY, account, _bytes(device), fingerprint[:8].ljust(8, b"\x00"))

    def read_eas_hierarchy_key(self, account: str, device: str, value: str) -> bytes:
        return self._read_compact(T_EAS_HIERARCHY, account, _bytes(device), value, 8)

    def device_hash(self, account: str, device: str) -> str:
        return hmac.new(self._key, b"device\x00" + _bytes(account) + b"\x00" + _bytes(device), hashlib.sha256).hexdigest()[:12]

    def account_throttle_hash(self, account: str, password: str) -> bytes:
        return hmac.new(self._key, b"throttle\x00" + _bytes(account) + b"\x00" + _bytes(password), hashlib.sha256).digest()

    def policy_key(self, account: str, device: str, phase: int) -> int:
        """Stateless Provision key (32 bit, != 0) per account, device and phase."""
        raw = hmac.new(self._key, b"policy\x00" + _bytes(account) + b"\x00" + _bytes(device) + b"\x00" + bytes([phase]), hashlib.sha256).digest()
        return int.from_bytes(raw[:4], "big") or 1
