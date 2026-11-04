"""HTTP parameters of an EAS request -> EasRequest ([MS-ASHTTP] 2.2.1.1.1). Pure.

Two query forms: plain text (`Cmd`, `User`, `DeviceId`, `DeviceType`) and Base64 (version, command code, locale, device id,
policy key, device type, parameters). The user value of the URL is never evaluated; the Basic header is authoritative.
A binary device id in the Base64 form is written in hexadecimal (instead of 400) so that devices with a GUID identifier do not fail.
"""

from __future__ import annotations

import base64
import binascii
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal
from urllib.parse import parse_qsl, unquote

COMMAND_CODES: Final[Mapping[int, str]] = MappingProxyType(
    {
        0: "Sync",
        1: "SendMail",
        2: "SmartForward",
        3: "SmartReply",
        4: "GetAttachment",
        9: "FolderSync",
        10: "FolderCreate",
        11: "FolderDelete",
        12: "FolderUpdate",
        13: "MoveItems",
        14: "GetItemEstimate",
        15: "MeetingResponse",
        16: "Search",
        17: "Settings",
        18: "Ping",
        19: "ItemOperations",
        20: "Provision",
        21: "ResolveRecipients",
        22: "ValidateCert",
        23: "Find",
    }
)
NAMES: Final = frozenset(COMMAND_CODES.values())
_DEVICE: Final = re.compile(r"[A-Za-z0-9]{1,32}")
_TYPE: Final = re.compile(r"[\x21-\x7e]{1,64}")
_VERSION: Final = re.compile(r"[0-9]{1,2}\.[0-9]{1,2}")
_POLICY: Final = re.compile(r"[0-9]{1,10}")
_PLAINTEXT: Final = re.compile(r"(?i)(^|&)(cmd|deviceid|devicetype|user)=")


class RequestError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


@dataclass(frozen=True, slots=True)
class EasRequest:
    method: Literal["OPTIONS", "POST", "GET"]
    command: str | None
    command_code: int | None
    version: str | None
    device: str | None
    device_type: str | None
    policy_key: int | None
    base64: bool


def _header(header: Mapping[str, str], name: str) -> str | None:
    for k, v in header.items():
        if k.lower() == name:
            return v.strip()
    return None


def _policy(value: str | None) -> int | None:
    if value is None or not _POLICY.fullmatch(value):
        return None
    number = int(value)
    return number if number <= 0xFFFFFFFF else None


def _base64_form(raw: str) -> tuple[str | None, int, str, str, int | None]:
    try:
        data = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise RequestError(400) from None
    if len(data) < 6:
        raise RequestError(400)
    version_byte, code = data[0], data[1]
    pos = 4
    length = data[pos]
    pos += 1
    if length < 1 or pos + length > len(data):
        raise RequestError(400)
    device_raw = data[pos : pos + length]
    pos += length
    if pos >= len(data):
        raise RequestError(400)
    plen = data[pos]
    pos += 1
    if plen not in (0, 4) or pos + plen > len(data):
        raise RequestError(400)
    policy = struct.unpack("<I", data[pos : pos + 4])[0] if plen == 4 else None
    pos += plen
    if pos >= len(data):
        raise RequestError(400)
    tlen = data[pos]
    pos += 1
    if tlen < 1 or pos + tlen > len(data):
        raise RequestError(400)
    try:
        type = data[pos : pos + tlen].decode("ascii")
    except UnicodeDecodeError:
        raise RequestError(400) from None
    try:
        device = device_raw.decode("ascii")
        if not _DEVICE.fullmatch(device):
            raise ValueError
    except (UnicodeDecodeError, ValueError):
        if len(device_raw) > 16:
            raise RequestError(400) from None
        device = device_raw.hex()
    version = f"{version_byte // 10}.{version_byte % 10}" if 10 <= version_byte <= 199 else None
    return version, code, device, type, policy


def read_request(method: str, raw_query: str, header: Mapping[str, str]) -> EasRequest:
    """Raises RequestError(400) on malformed forms, (405) on a wrong method. Unknown command codes => command None, code set."""
    m = method.upper()
    if m not in ("OPTIONS", "POST", "GET"):
        raise RequestError(405)
    if m == "OPTIONS":
        return EasRequest("OPTIONS", None, None, _header(header, "ms-asprotocolversion"), None, None, None, False)
    query = raw_query or ""
    if len(query) > 2048:
        raise RequestError(400)
    if query and not _PLAINTEXT.search(query):
        version, code, device, type, policy = _base64_form(unquote(query))
        command = COMMAND_CODES.get(code)
        if not _TYPE.fullmatch(type):
            raise RequestError(400)
        if policy is None:
            policy = _policy(_header(header, "x-ms-policykey"))
        return EasRequest(m, command, code, version, device, type, policy, True)  # type: ignore[arg-type]
    values: dict[str, str] = {}
    for k, v in parse_qsl(query, keep_blank_values=True, max_num_fields=32):
        values.setdefault(k.lower(), v)
    command_text = values.get("cmd")
    device = values.get("deviceid")
    type = values.get("devicetype")
    if m == "GET" and command_text is None:
        return EasRequest("GET", None, None, None, None, None, None, False)
    if not command_text or not device or not type:
        raise RequestError(400)
    if not _DEVICE.fullmatch(device) or not _TYPE.fullmatch(type):
        raise RequestError(400)
    code = next((c for c, n in COMMAND_CODES.items() if n.lower() == command_text.lower()), None)
    command = COMMAND_CODES[code] if code is not None else None
    version = _header(header, "ms-asprotocolversion")
    if version is not None and not _VERSION.fullmatch(version):
        raise RequestError(400)
    return EasRequest(m, command, code, version, device, type, _policy(_header(header, "x-ms-policykey")), False)  # type: ignore[arg-type]
