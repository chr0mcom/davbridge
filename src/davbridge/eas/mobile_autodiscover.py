"""Autodiscover for mobile devices: JSON v2 and POX `mobilesync`, anonymous and account-neutral. Pure except for the response objects.

Within the configured domains the answer is the same for every local part (no account oracle); the URL comes from the
configuration, never from the Host header. No `WWW-Authenticate` line in this group."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final
from urllib.parse import parse_qsl, unquote

from lxml import etree

from ..config import Config

RESP_NS: Final = "http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006"
MOBILE_RESP_NS: Final = "http://schemas.microsoft.com/exchange/autodiscover/mobilesync/responseschema/2006"
MOBILE_REQ: Final = b"autodiscover/mobilesync/requestschema/2006"
MAX_POX: Final = 16 * 1024
_ADDRESS: Final = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}")
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, remove_comments=True)


def domains(config: Config) -> frozenset[str]:
    d = {k.address.rsplit("@", 1)[-1].lower() for k in config.accounts}
    if "@" in config.address_template:
        d.add(config.address_template.rsplit("@", 1)[-1].lower())
    d.add(config.public_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower())
    return frozenset(d)


def eas_url(config: Config) -> str:
    return f"{config.public_url}/Microsoft-Server-ActiveSync"


def json_response(config: Config, path: str, query: str) -> tuple[int, bytes, str | None]:
    """-> (HTTP status, JSON body, protocol value for the log)."""
    values: dict[str, str] = {}
    for k, v in parse_qsl(query or "", keep_blank_values=True, max_num_fields=16):
        values.setdefault(k.lower(), v)
    address = values.get("email")
    parts = [t for t in path.split("/") if t]
    if len(parts) >= 4 and parts[2].lower() == "v1.0":
        address = unquote(parts[3])
    log = (values.get("protocol") or "").strip()
    if not address or not _ADDRESS.fullmatch(address) or address.rsplit("@", 1)[-1].lower() not in domains(config):
        return 404, b"{}", log[:24] or None
    p = log.lower()
    if p == "activesync":
        content = {"Protocol": log if log in ("ActiveSync", "activesync", "Activesync", "ACTIVESYNC") else "ActiveSync", "Url": eas_url(config)}
        return 200, json.dumps(content).encode(), "ActiveSync"
    if p in ("autodiscoverv1", "autodiscover"):
        content = {"Protocol": "AutodiscoverV1", "Url": f"{config.public_url}/autodiscover/autodiscover.xml"}
        return 200, json.dumps(content).encode(), "AutodiscoverV1"
    error = {"ErrorCode": "InvalidProtocol", "ErrorMessage": "The given protocol is not supported."}
    return 400, json.dumps(error).encode(), None


def is_mobilesync(body: bytes) -> bool:
    return MOBILE_REQ in body[:MAX_POX]


def _address_from(body: bytes) -> str | None:
    if not body or len(body) > MAX_POX or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        return None
    try:
        root = etree.fromstring(body, _PARSER)
    except etree.XMLSyntaxError:
        return None
    for e in root.iter():
        if isinstance(e.tag, str) and etree.QName(e).localname == "EMailAddress" and e.text:
            return e.text.strip()
    return None


def _kind(parents: etree._Element, name: str, text: str | None = None) -> etree._Element:
    e = etree.SubElement(parents, f"{{{MOBILE_RESP_NS}}}{name}")
    if text is not None:
        e.text = text
    return e


def pox_response(config: Config, body: bytes) -> bytes:
    address = _address_from(body)
    root = etree.Element(f"{{{RESP_NS}}}Autodiscover", nsmap={None: RESP_NS})
    resp = etree.SubElement(root, f"{{{MOBILE_RESP_NS}}}Response", nsmap={None: MOBILE_RESP_NS})
    if address is None or not _ADDRESS.fullmatch(address) or address.rsplit("@", 1)[-1].lower() not in domains(config):
        error = _kind(resp, "Error")
        _kind(error, "ErrorCode", "600")
        _kind(error, "Message", "Invalid Request")
        _kind(error, "DebugData")
        return etree.tostring(root, xml_declaration=True, encoding="utf-8")
    _kind(resp, "Culture", config.autodiscover_culture)
    user = _kind(resp, "User")
    _kind(user, "DisplayName", address)
    _kind(user, "EMailAddress", address)
    settings = _kind(_kind(resp, "Action"), "Settings")
    server = _kind(settings, "Server")
    _kind(server, "Type", "MobileSync")
    _kind(server, "Url", eas_url(config))
    _kind(server, "Name", eas_url(config))
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def query_names(query: str) -> str:
    """Only parameter names for the log, never values."""
    names = sorted({k for k, _ in parse_qsl(query or "", keep_blank_values=True, max_num_fields=16) if re.fullmatch(r"[A-Za-z0-9_\-]{1,20}", k)})
    return ",".join(names)[:64]


def path_segments(path: str) -> str | None:
    seg = [s for s in path.split("/") if s][:4]
    clean = [s if re.fullmatch(r"[A-Za-z0-9._\-]{1,40}", s) else "x" for s in seg]
    return ":".join(clean) or None


def header(values: Mapping[str, str]) -> dict[str, str]:
    return {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", **values}
