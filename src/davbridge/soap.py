"""SOAP/EWS envelope: hardened parsing and response building. Pure module: no I/O, no asyncio, no mutable globals.

Response XML is built exclusively from lxml elements (values are escaped), never by string formatting.
Exceptions never carry user data (no str(e)); callers log only the type name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping, Sequence

from lxml import etree

from .errors import Code, text_for

SOAP_NS: Final = "http://schemas.xmlsoap.org/soap/envelope/"
MESSAGES_NS: Final = "http://schemas.microsoft.com/exchange/services/2006/messages"
TYPES_NS: Final = "http://schemas.microsoft.com/exchange/services/2006/types"
ERRORS_NS: Final = "http://schemas.microsoft.com/exchange/services/2006/errors"
NS: Final[Mapping[str, str]] = MappingProxyType({"s": SOAP_NS, "m": MESSAGES_NS, "t": TYPES_NS})
_NSMAP: Final = dict(NS)  # used read-only; lxml copies on element construction

MAX_BODY: Final = 1 << 20
SERVER_VERSION: Final[Mapping[str, str]] = MappingProxyType(dict(MajorVersion="15", MinorVersion="1", MajorBuildNumber="2507", MinorBuildNumber="6", Version="V2016_10_10"))

_OPERATION: Final = re.compile(r"[A-Za-z]{1,64}")
_VERSION: Final = re.compile(r"[A-Za-z0-9_.\-]{1,30}")
_INVALID: Final = re.compile("[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]")


@dataclass(frozen=True, slots=True)
class Request:
    operation: str  # local name of the first Body child, [A-Za-z]{1,64}
    payload: etree._Element  # the operation element
    version: str | None  # RequestServerVersion/@Version
    timezone: str | None  # TimeZoneContext/TimeZoneDefinition/@Id (Windows-ID)
    impersonation: bool


class SoapError(Exception):
    """-> SOAP fault (HTTP 500) with the ResponseCode in the detail."""

    def __init__(self, code: Code) -> None:
        super().__init__(code.value)
        self.code = code


def _parser() -> etree.XMLParser:
    # New per call: lxml parsers are not thread-safe.
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False, huge_tree=False, remove_comments=True, remove_pis=True, collect_ids=False)


def read(body: bytes, soap_action: str | None) -> Request:
    if len(body) > MAX_BODY:
        raise SoapError(Code.ErrorSchemaValidation)
    above = body.upper()
    # Second line of defence before lxml: EWS requests never contain a DTD or entities. NUL bytes suggest UTF-16/32 and thus an attempt to bypass
    # the text check.
    if b"<!DOCTYPE" in above or b"<!ENTITY" in above or b"\x00" in body:
        raise SoapError(Code.ErrorSchemaValidation)
    try:
        root = etree.fromstring(body, _parser())
    except (etree.XMLSyntaxError, ValueError):
        raise SoapError(Code.ErrorSchemaValidation) from None
    tree = root.getroottree()
    if tree.docinfo.doctype or tree.docinfo.internalDTD is not None or tree.docinfo.externalDTD is not None:
        raise SoapError(Code.ErrorSchemaValidation)
    for _ in root.iter(etree.Entity):
        raise SoapError(Code.ErrorSchemaValidation)
    if root.tag != f"{{{SOAP_NS}}}Envelope":
        raise SoapError(Code.ErrorSchemaValidation)
    soap_body = root.find(f"{{{SOAP_NS}}}Body")
    if soap_body is None or len(soap_body) == 0:
        raise SoapError(Code.ErrorSchemaValidation)
    payload = soap_body[0]
    if not isinstance(payload.tag, str):
        raise SoapError(Code.ErrorSchemaValidation)
    qname = etree.QName(payload)
    if qname.namespace != MESSAGES_NS or not _OPERATION.fullmatch(qname.localname):
        raise SoapError(Code.ErrorSchemaValidation)
    operation = qname.localname
    desired = (soap_action or "").strip().strip('"').strip()
    if desired and desired != f"{MESSAGES_NS}/{operation}":
        raise SoapError(Code.ErrorInvalidRequest)
    version = timezone = None
    impersonation = False
    header = root.find(f"{{{SOAP_NS}}}Header")
    if header is not None:
        value = header.find(f"{{{TYPES_NS}}}RequestServerVersion")
        if value is not None and _VERSION.fullmatch(value.get("Version", "")):
            version = value.get("Version")
        zone = header.find(f"{{{TYPES_NS}}}TimeZoneContext/{{{TYPES_NS}}}TimeZoneDefinition")
        if zone is not None and zone.get("Id"):
            timezone = zone.get("Id")[:100]
        impersonation = header.find(f"{{{TYPES_NS}}}ExchangeImpersonation") is not None
    return Request(operation, payload, version, timezone, impersonation)


# ---------------------------------------------------------------- building


def clean(text: str) -> str:
    """Removes characters that are forbidden in XML 1.0 (lxml would raise ValueError otherwise)."""
    return _INVALID.sub("", text)


def elem(ns: str, name: str, text: str | None = None, attrib: Mapping[str, str] | None = None, children: Sequence[etree._Element] = ()) -> etree._Element:
    """An element in the namespace `ns` (SOAP_NS/MESSAGES_NS/TYPES_NS). Text and attributes are cleaned; lxml escapes them."""
    e = etree.Element(f"{{{ns}}}{name}", nsmap=_NSMAP)
    if attrib:
        for k, v in attrib.items():
            e.set(k, clean(str(v)))
    if text is not None:
        e.text = clean(text)
    for k in children:
        e.append(k)
    return e


def m(name: str, text: str | None = None, **attrib: str) -> etree._Element:
    return elem(MESSAGES_NS, name, text, attrib or None)


def t(name: str, text: str | None = None, **attrib: str) -> etree._Element:
    return elem(TYPES_NS, name, text, attrib or None)


def message(operation: str, code: Code = Code.NoError, *content: etree._Element, backoff_ms: int | None = None) -> etree._Element:
    """One `<m:{operation}ResponseMessage>`. Exactly one per requested folder/item, in request order."""
    if not _OPERATION.fullmatch(operation):
        raise ValueError("operation")
    error = code is not Code.NoError
    n = m(f"{operation}ResponseMessage", ResponseClass="Error" if error else "Success")
    if error:
        n.append(m("MessageText", text_for(code)))
    n.append(m("ResponseCode", code.value))
    if error:
        n.append(m("DescriptiveLinkKey", "0"))
        if backoff_ms is not None:
            n.append(elem(MESSAGES_NS, "MessageXml", children=[t("Value", str(int(backoff_ms)), Name="BackOffMilliseconds")]))
    for k in content:
        n.append(k)
    return n


def _envelope(root: etree._Element, header_extra: Sequence[etree._Element]) -> bytes:
    env = elem(SOAP_NS, "Envelope")
    header = elem(SOAP_NS, "Header", children=[elem(TYPES_NS, "ServerVersionInfo", attrib=SERVER_VERSION), *header_extra])
    env.append(header)
    env.append(elem(SOAP_NS, "Body", children=[root]))
    etree.cleanup_namespaces(env, top_nsmap=_NSMAP)
    return etree.tostring(env, xml_declaration=True, encoding="utf-8")


def response_with_root(root: etree._Element, *, header_extra: Sequence[etree._Element] = ()) -> bytes:
    """For operations with a different response shape (for example GetDelegate): the caller builds the `<m:...Response>` itself."""
    return _envelope(root, header_extra)


def response(operation: str, messages: Sequence[etree._Element], *, header_extra: Sequence[etree._Element] = ()) -> bytes:
    if not _OPERATION.fullmatch(operation):
        raise ValueError("operation")
    root = m(f"{operation}Response")
    root.append(elem(MESSAGES_NS, "ResponseMessages", children=list(messages)))
    return _envelope(root, header_extra)


def response_error(operation: str, code: Code, *, backoff_ms: int | None = None) -> bytes:
    """A single error message as a complete response (unknown operation, exception outside the element loop)."""
    return response(operation, [message(operation, code, backoff_ms=backoff_ms)])


def stream_chunk(messages: Sequence[etree._Element], *, first: bool = False) -> bytes:
    """One envelope per chunk for GetStreamingEvents. The XML declaration appears only in the first chunk: the stream is a sequence of
    envelopes as on Exchange; a second declaration in the middle of the stream is a syntax error for a fragment reader."""
    content = response("GetStreamingEvents", messages).rstrip() + b"\n"
    if first:
        return content
    return content[content.index(b"?>") + 2 :].lstrip() if content.startswith(b"<?xml") else content


def fault(code: Code) -> bytes:
    e = {"e": ERRORS_NS}
    f = etree.Element(f"{{{SOAP_NS}}}Fault", nsmap=_NSMAP)
    fc = etree.SubElement(f, "faultcode")
    fc.text = f"t:{code.value}"  # prefix t is bound to TYPES_NS at the root
    fs = etree.SubElement(f, "faultstring")
    fs.set("{http://www.w3.org/XML/1998/namespace}lang", "en-US")
    fs.text = clean(text_for(code))
    detail = etree.SubElement(f, "detail")
    rc = etree.SubElement(detail, f"{{{ERRORS_NS}}}ResponseCode", nsmap=e)
    rc.text = code.value
    msg = etree.SubElement(detail, f"{{{ERRORS_NS}}}Message", nsmap=e)
    msg.text = clean(text_for(code))
    env = elem(SOAP_NS, "Envelope", children=[elem(SOAP_NS, "Body", children=[f])])
    return etree.tostring(env, xml_declaration=True, encoding="utf-8")
