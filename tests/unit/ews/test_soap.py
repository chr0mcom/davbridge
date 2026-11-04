import pytest
from lxml import etree

from davbridge import soap
from davbridge.errors import Code
from davbridge.soap import MESSAGES_NS, NS, SOAP_NS, TYPES_NS, SoapError

HEADER = f'xmlns:s="{SOAP_NS}" xmlns:t="{TYPES_NS}" xmlns:m="{MESSAGES_NS}"'


def envelope(inner, header="", prolog=""):
    return (f"{prolog}<s:Envelope {HEADER}><s:Header>{header}</s:Header><s:Body>{inner}</s:Body></s:Envelope>").encode()


def error_code(body, action=None):
    with pytest.raises(SoapError) as e:
        soap.read(body, action)
    return e.value.code


def test_read_finds_operation_and_head():
    header = '<t:RequestServerVersion Version="Exchange2016"/><t:TimeZoneContext><t:TimeZoneDefinition Id="W. Europe Standard Time"/></t:TimeZoneContext>'
    a = soap.read(envelope("<m:GetFolder/>", header), f"{MESSAGES_NS}/GetFolder")
    assert (a.operation, a.version, a.timezone, a.impersonation) == ("GetFolder", "Exchange2016", "W. Europe Standard Time", False)
    assert a.payload.tag == f"{{{MESSAGES_NS}}}GetFolder"


def test_impersonation_is_detected():
    header = "<t:ExchangeImpersonation><t:ConnectingSID/></t:ExchangeImpersonation>"
    assert soap.read(envelope("<m:GetFolder/>", header), None).impersonation is True


def test_soapaction_with_quotes_matches():
    assert soap.read(envelope("<m:GetFolder/>"), f'"{MESSAGES_NS}/GetFolder"').operation == "GetFolder"


@pytest.mark.parametrize("action", [f"{MESSAGES_NS}/FindItem", "http://evil/GetFolder"])
def test_wrong_soapaction_is_refused(action):
    assert error_code(envelope("<m:GetFolder/>"), action) is Code.ErrorInvalidRequest


@pytest.mark.parametrize("action", [None, "", '""', "  "])
def test_missing_soapaction_is_ok(action):
    assert soap.read(envelope("<m:GetFolder/>"), action).operation == "GetFolder"


def test_unknown_operation_is_read_dispatch_decides():
    assert soap.read(envelope("<m:UnknownOperation/>"), None).operation == "UnknownOperation"


def test_operation_outside_the_messages_namespace():
    assert error_code(envelope('<x:GetFolder xmlns:x="urn:x"/>'), None) is Code.ErrorSchemaValidation


def test_invalid_operation_name_is_refused():
    assert error_code(envelope("<m:Get_Folder/>"), None) is Code.ErrorSchemaValidation
    assert error_code(envelope("<m:" + "A" * 65 + "/>"), None) is Code.ErrorSchemaValidation


XXE = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
BOMB = '<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'


@pytest.mark.parametrize("prolog", [XXE, BOMB, "<!doctype x>", "<!DocType x>"])
def test_doctype_and_entities_are_rejected(prolog):
    assert error_code(envelope("<m:GetFolder/>", prolog=prolog), None) is Code.ErrorSchemaValidation


def test_entity_reference_in_text_is_rejected():
    assert error_code(envelope("<m:GetFolder>&x;</m:GetFolder>"), None) is Code.ErrorSchemaValidation


def test_utf16_bypass_is_refused():
    raw = (XXE + envelope("<m:GetFolder/>").decode()).encode("utf-16")
    assert error_code(raw, None) is Code.ErrorSchemaValidation


def test_too_large_body():
    assert error_code(b" " * (soap.MAX_BODY + 1), None) is Code.ErrorSchemaValidation


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not xml",
        b"<a><b></a>",
        envelope("")[:-30],
        b'<s:Envelope xmlns:s="x"/>',
        f'<s:Envelope xmlns:s="{SOAP_NS}"/>'.encode(),
        f'<s:Envelope xmlns:s="{SOAP_NS}"><s:Body/></s:Envelope>'.encode(),
        f'<m:GetFolder xmlns:m="{MESSAGES_NS}"/>'.encode(),
    ],
)
def test_broken_envelopes(body):
    assert error_code(body, None) is Code.ErrorSchemaValidation


def test_comments_and_pis_do_not_disturb():
    a = soap.read(envelope("<!-- x --><?pi y?><m:GetFolder/>"), None)
    assert a.operation == "GetFolder"


def test_error_texts_contain_no_user_data():
    try:
        soap.read(b"<secret>password123", None)
    except SoapError as e:
        assert "password123" not in repr(e) and "password123" not in str(e)
        assert e.__cause__ is None


# ---------------------------------------------------------------- building


def parse(b):
    return etree.fromstring(b)


def test_response_contains_server_version_and_messages_in_order():
    n = [soap.message("GetFolder", Code.NoError, soap.m("Folders")), soap.message("GetFolder", Code.ErrorFolderNotFound), soap.message("GetFolder", Code.NoError)]
    r = parse(soap.response("GetFolder", n))
    assert r.find("s:Header/t:ServerVersionInfo", NS).get("Version") == "V2016_10_10"
    parts = r.findall("s:Body/m:GetFolderResponse/m:ResponseMessages/m:GetFolderResponseMessage", NS)
    assert [x.get("ResponseClass") for x in parts] == ["Success", "Error", "Success"]
    assert [x.findtext("m:ResponseCode", namespaces=NS) for x in parts] == ["NoError", "ErrorFolderNotFound", "NoError"]


def test_error_message_has_text_link_and_backoff():
    n = soap.message("SyncFolderItems", Code.ErrorServerBusy, backoff_ms=30000)
    assert [etree.QName(k).localname for k in n] == ["MessageText", "ResponseCode", "DescriptiveLinkKey", "MessageXml"]
    value = n.find("m:MessageXml/t:Value", NS)
    assert value.get("Name") == "BackOffMilliseconds" and value.text == "30000"


def test_success_message_has_only_code_and_content():
    n = soap.message("GetItem", Code.NoError, soap.m("Items"))
    assert [etree.QName(k).localname for k in n] == ["ResponseCode", "Items"]


def test_user_values_are_escaped_never_become_markup():
    malicious = '</t:Subject><m:Evil a="1"/>&<>"\''
    el = soap.t("Subject", malicious)
    items = soap.elem(MESSAGES_NS, "Items", children=[el])
    r = parse(soap.response("GetItem", [soap.message("GetItem", Code.NoError, items)]))
    assert r.find(".//t:Subject", NS).text == malicious
    assert r.find(".//m:Evil", NS) is None


def test_attributes_are_escaped():
    el = soap.t("FolderId", Id='"><x/>')
    assert el.get("Id") == '"><x/>'
    assert b"<x/>" not in etree.tostring(el)


def test_forbidden_xml_chars_are_removed():
    el = soap.t("Subject", "a\x00b\x01c\x0bd\ud800e￿f")
    b = etree.tostring(el)
    assert etree.fromstring(b).text == "abcdef"


def test_stream_chunk_is_complete_envelope():
    c = soap.stream_chunk([soap.message("GetStreamingEvents", Code.NoError, soap.m("ConnectionStatus", "OK"))])
    r = parse(c)
    assert r.find("s:Body/m:GetStreamingEventsResponse/m:ResponseMessages/m:GetStreamingEventsResponseMessage/m:ConnectionStatus", NS).text == "OK"
    assert c.startswith(b"<s:Envelope")
    first = soap.stream_chunk([soap.message("GetStreamingEvents", Code.NoError)], first=True)
    assert first.startswith(b"<?xml")
    from lxml import etree

    reader = etree.XMLPullParser(events=("end",))
    reader.feed(b"<strom>" + first[first.index(b"?>") + 2 :] + c + c + b"</strom>")
    assert sum(1 for _, e in reader.read_events() if etree.QName(e).localname == "Envelope") == 3


def test_fault_has_response_code_in_detail():
    r = parse(soap.fault(Code.ErrorSchemaValidation))
    assert r.find("s:Body/s:Fault/detail/{http://schemas.microsoft.com/exchange/services/2006/errors}ResponseCode", NS).text == "ErrorSchemaValidation"
    assert r.find("s:Body/s:Fault/faultcode", NS).text == "t:ErrorSchemaValidation"


def test_response_error_single_report():
    r = parse(soap.response_error("Unknown", Code.ErrorInvalidRequest))
    assert r.findtext(".//m:ResponseCode", namespaces=NS) == "ErrorInvalidRequest"


def test_response_with_root_for_deviating_form():
    w = soap.m("GetDelegateResponse", ResponseClass="Success")
    w.append(soap.m("ResponseCode", "NoError"))
    r = parse(soap.response_with_root(w))
    assert r.find("s:Body/m:GetDelegateResponse", NS).get("ResponseClass") == "Success"


def test_invalid_operation_names_at_construction():
    for name in ["", "A B", "A/B", "<x>", "A" * 65]:
        with pytest.raises(ValueError):
            soap.response(name, [])
        with pytest.raises(ValueError):
            soap.message(name)


def test_no_mutable_globals():
    with pytest.raises(TypeError):
        soap.NS["x"] = "y"
    with pytest.raises(TypeError):
        soap.SERVER_VERSION["x"] = "y"
