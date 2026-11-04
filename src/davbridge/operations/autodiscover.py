"""POX autodiscover: only for the address of the signed-in account; EwsUrl comes from the configuration."""

from __future__ import annotations

from lxml import etree

from ..config import Account, Config

REQ_NS = "http://schemas.microsoft.com/exchange/autodiscover/outlook/requestschema/2006"
RESP_NS = "http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006"
OUTLOOK_NS = "http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a"
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, remove_comments=True)


def requested_address(body: bytes) -> str | None:
    if not body or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        return None
    try:
        root = etree.fromstring(body, PARSER)
    except etree.XMLSyntaxError:
        return None
    for e in root.iter():
        if isinstance(e.tag, str) and etree.QName(e).localname == "EMailAddress" and e.text:
            return e.text.strip().lower()
    return None


def _kind(parents: etree._Element, ns: str, name: str, text: str | None = None) -> etree._Element:
    k = etree.SubElement(parents, f"{{{ns}}}{name}")
    if text is not None:
        k.text = text
    return k


def response(config: Config, account: Account) -> bytes:
    root = etree.Element(f"{{{RESP_NS}}}Autodiscover", nsmap={None: RESP_NS})
    resp = etree.SubElement(root, f"{{{OUTLOOK_NS}}}Response", nsmap={None: OUTLOOK_NS})
    user = _kind(resp, OUTLOOK_NS, "User")
    _kind(user, OUTLOOK_NS, "DisplayName", account.display_name)
    _kind(user, OUTLOOK_NS, "AutoDiscoverSMTPAddress", account.address)
    acc = _kind(resp, OUTLOOK_NS, "Account")
    _kind(acc, OUTLOOK_NS, "AccountType", "email")
    _kind(acc, OUTLOOK_NS, "Action", "settings")
    proto = _kind(acc, OUTLOOK_NS, "Protocol")
    ews = f"{config.public_url}/EWS/Exchange.asmx"
    host = config.public_url.split("://", 1)[-1]
    _kind(proto, OUTLOOK_NS, "Type", "EXPR")
    _kind(proto, OUTLOOK_NS, "Server", host)
    _kind(proto, OUTLOOK_NS, "SSL", "On")
    _kind(proto, OUTLOOK_NS, "AuthPackage", "Basic")
    _kind(proto, OUTLOOK_NS, "EwsUrl", ews)
    _kind(proto, OUTLOOK_NS, "ASUrl", ews)
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def error() -> bytes:
    root = etree.Element(f"{{{RESP_NS}}}Autodiscover", nsmap={None: RESP_NS})
    resp = etree.SubElement(root, f"{{{OUTLOOK_NS}}}Response", nsmap={None: OUTLOOK_NS})
    err = _kind(resp, OUTLOOK_NS, "Error")
    _kind(err, OUTLOOK_NS, "ErrorCode", "500")
    _kind(err, OUTLOOK_NS, "Message", "The e-mail address cannot be found.")
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")
