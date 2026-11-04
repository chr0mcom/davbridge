import asyncio
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from lxml import etree

from davbridge import protocol_capture, soap
from davbridge.soap import MESSAGES_NS, SOAP_NS, TYPES_NS

HEADER = f'xmlns:s="{SOAP_NS}" xmlns:t="{TYPES_NS}" xmlns:m="{MESSAGES_NS}"'
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

SECRETS = [
    "Dentist Dr. Müller private",
    "Praxis Hauptstr. 12",
    "Password hunter2 in the text",
    "max.mustermann@example.org",
    "d5b3c1a0-1111-4222-8333-944444444444",
    "Vacation Secret",
    "Authorization-Wert-XYZ",
    "hmac-key-QQQ",
]


def request(inner, header=""):
    b = f"<s:Envelope {HEADER}><s:Header>{header}</s:Header><s:Body>{inner}</s:Body></s:Envelope>".encode()
    return soap.read(b, None)


def serialized(s):
    return json.dumps(protocol_capture.skeleton_as_dict(s), ensure_ascii=False) + repr(s)


def create_item():
    return request(
        f"""<m:CreateItem SendMeetingInvitations="SendToNone" MessageDisposition="SaveOnly">
      <m:SavedItemFolderId><t:DistinguishedFolderId Id="calendar"/></m:SavedItemFolderId>
      <m:Items><t:CalendarItem>
        <t:Subject>{SECRETS[0]}</t:Subject><t:Body BodyType="Text">{SECRETS[2]}</t:Body>
        <t:ReminderIsSet>false</t:ReminderIsSet><t:Start>2026-11-04T09:00:00Z</t:Start><t:End>2026-11-04T09:30:00Z</t:End>
        <t:Location>{SECRETS[1]}</t:Location><t:UID>{SECRETS[4]}</t:UID>
        <t:Categories><t:String>{SECRETS[5]}</t:String></t:Categories>
        <t:RequiredAttendees><t:Attendee><t:Mailbox><t:EmailAddress>{SECRETS[3]}</t:EmailAddress></t:Mailbox></t:Attendee></t:RequiredAttendees>
        <t:LegacyFreeBusyStatus>Busy</t:LegacyFreeBusyStatus><t:Sensitivity>Normal</t:Sensitivity>
        <t:StartTimeZone Id="W. Europe Standard Time"/>
      </t:CalendarItem></m:Items></m:CreateItem>""",
        '<t:RequestServerVersion Version="Exchange2016"/>',
    )


HEADER_LINES = {
    "Authorization": "Basic " + SECRETS[6],
    "Cookie": "s=" + SECRETS[7],
    "User-Agent": "MacOutlook/16.114.1 (Intel Mac OS X 27.0.1)",
    "Content-Type": "text/xml; charset=utf-8",
    "SOAPAction": f'"{MESSAGES_NS}/CreateItem"',
    "X-Forwarded-For": "203.0.113.9",
    "X-Secret": SECRETS[7],
    "Content-Length": "1234",
}


def test_skeleton_contains_no_secrets():
    s = protocol_capture.skeleton(create_item(), HEADER_LINES, NOW)
    text = serialized(s)
    for g in SECRETS + ["203.0.113.9", "Basic "]:
        assert g not in text, g
    assert "Müller" not in text and "hunter2" not in text


def test_skeleton_shows_structure_and_allowed_values():
    s = protocol_capture.skeleton(create_item(), HEADER_LINES, NOW)
    d = protocol_capture.skeleton_as_dict(s)
    text = json.dumps(d, ensure_ascii=False)
    assert s.operation == "CreateItem"
    assert '"m:CreateItem"' in text and '"t:CalendarItem"' in text and '"t:UID"' in text
    assert ["SendMeetingInvitations", "SendToNone"] in _all_attribute(d["tree"])
    assert ["Id", "calendar"] in _all_attribute(d["tree"])
    assert ["#text", "Busy"] in _all_attribute(d["tree"])
    assert ["#text", "<time>"] in _all_attribute(d["tree"])
    assert ["#text", "<uid:uuid>"] in _all_attribute(d["tree"])
    assert ["#text", "<text:17-256>"] in _all_attribute(d["tree"]) or ["#text", "<text:1-16>"] in _all_attribute(d["tree"])
    assert ["Id", "W. Europe Standard Time"] not in _all_attribute(d["tree"])  # StartTimeZone is not a TimeZoneDefinition


def _all_attribute(node):
    out = [list(a) for a in node["attribute"]]
    for k in node["children"]:
        out += _all_attribute(k)
    return out


def test_head_only_names_and_allowed_values():
    s = protocol_capture.skeleton(create_item(), HEADER_LINES, NOW)
    header = dict(s.header)
    assert "authorization" not in header and "cookie" not in header and "x-forwarded-for" not in header
    assert header["user-agent"] == "MacOutlook/16.114.1"
    assert header["soapaction"] == "messages/CreateItem"
    assert header["content-type"] == "text/xml"
    assert header["x-secret"] == "" and header["content-length"] == ""


def test_calendar_view_only_relative_time_windows():
    a = request(
        '<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape></m:ItemShape>'
        '<m:CalendarView MaxEntriesReturned="200" StartDate="2026-11-08T12:00:00Z" EndDate="2026-12-08T12:00:00Z"/>'
        '<m:ParentFolderIds><t:DistinguishedFolderId Id="calendar"/></m:ParentFolderIds></m:FindItem>'
    )
    d = protocol_capture.skeleton_as_dict(protocol_capture.skeleton(a, {}, NOW))
    attrs = _all_attribute(d["tree"])
    assert ["start_days", "7"] in attrs and ["duration_days", "30"] in attrs
    text = json.dumps(d)
    assert "2026-11-08" not in text and "2026-12-08" not in text
    assert ["MaxEntriesReturned", "200"] in attrs and ["#text", "IdOnly"] in attrs and ["Traversal", "Shallow"] in attrs


def test_fingerprint_ignores_time_window_not_structure():
    def view(start, end, extra=""):
        return request(f'<m:FindItem Traversal="Shallow"><m:CalendarView StartDate="{start}" EndDate="{end}"/>{extra}</m:FindItem>')

    a = protocol_capture.skeleton(view("2026-11-01T00:00:00Z", "2026-12-01T00:00:00Z"), {}, NOW)
    b = protocol_capture.skeleton(view("2026-02-01T00:00:00Z", "2026-02-10T00:00:00Z"), {}, NOW)
    c = protocol_capture.skeleton(view("2026-11-01T00:00:00Z", "2026-12-01T00:00:00Z", "<m:New/>"), {}, NOW)
    assert a.fingerprint == b.fingerprint != c.fingerprint
    assert len(a.fingerprint) == 16 and a.tree != b.tree


def test_ids_syncstate_classes_without_and_with_checker():
    a = request(
        '<m:SyncFolderItems><m:SyncFolderId><t:FolderId Id="QUJDREVGR0hJSktMTU5PUA==" ChangeKey="kurz!"/></m:SyncFolderId>'
        "<m:SyncState>QUJDREVGR0hJSktMTU5PUA</m:SyncState><m:MaxChangesReturned>100</m:MaxChangesReturned></m:SyncFolderItems>"
    )
    attrs = _all_attribute(protocol_capture.skeleton_as_dict(protocol_capture.skeleton(a, {}, NOW))["tree"])
    assert ["Id", "<id:valid>"] in attrs and ["ChangeKey", "<id:invalid>"] in attrs
    assert ["#text", "<id:valid>"] in attrs and ["#text", "100"] in attrs
    calls = []
    s = protocol_capture.skeleton(a, {}, NOW, id_checker=lambda kind, value: calls.append(kind) or False)
    attrs = _all_attribute(protocol_capture.skeleton_as_dict(s)["tree"])
    assert ["Id", "<id:invalid>"] in attrs and {"folder", "changekey", "sync"} <= set(calls)


def test_empty_syncstate_is_empty():
    a = request("<m:SyncFolderItems><m:SyncState/></m:SyncFolderItems>")
    assert ["#text", "<empty>"] in _all_attribute(protocol_capture.skeleton_as_dict(protocol_capture.skeleton(a, {}, NOW))["tree"])


def test_checker_that_raises_does_not_disturb():
    a = request('<m:GetItem><t:ItemId Id="QUJDREVGR0hJSktMTU5PUA"/></m:GetItem>')

    def malicious(kind, value):
        raise RuntimeError(value)

    s = protocol_capture.skeleton(a, {}, NOW, id_checker=malicious)
    assert "<id:invalid>" in json.dumps(protocol_capture.skeleton_as_dict(s), ensure_ascii=False)


def test_skeleton_never_raises():
    broken = SimpleNamespace(operation="X", payload=None)
    s = protocol_capture.skeleton(broken, {"a": object()}, NOW)
    assert s.operation in ("<error>", "X") and len(s.fingerprint) == 16
    assert protocol_capture.skeleton(create_item(), {1: 2, None: "x"}, NOW).operation == "CreateItem"


def test_depth_and_width_are_bounded():
    deep = "<m:X>" * 200 + "</m:X>" * 200
    d = protocol_capture.skeleton_as_dict(protocol_capture.skeleton(request(deep), {}, NOW))
    assert "<too-deep>" in json.dumps(d)
    wide = "<m:Op>" + "<m:K/>" * 600 + "</m:Op>"
    s = protocol_capture.skeleton(request(wide), {}, NOW)
    assert len(s.tree[2]) == protocol_capture.CHILDREN_MAX + 1 and s.tree[2][-1][0] == "<truncated>"


def test_foreign_names_become_classes():
    a = request('<m:Op><x:A xmlns:x="urn:ok:ns" b="1"/><y:B xmlns:y="urn:secret:%C3%BC"/></m:Op>')
    text = json.dumps(protocol_capture.skeleton_as_dict(protocol_capture.skeleton(a, {}, NOW)))
    assert "{urn:ok:ns}A" in text and "{<ns>}B" in text and "secret" not in text


# ---------------------------------------------------------------- Fuzz

NAMES = [
    "BaseShape",
    "EventType",
    "LegacyFreeBusyStatus",
    "Sensitivity",
    "Importance",
    "BodyType",
    "MaxChangesReturned",
    "ConnectionTimeout",
    "Timeout",
    "MaxEntriesReturned",
    "Offset",
    "ReminderMinutesBeforeStart",
    "SyncState",
    "SubscriptionId",
    "Watermark",
    "UID",
    "Subject",
    "Location",
    "Body",
    "EmailAddress",
    "String",
    "Start",
    "End",
    "Constant",
    "Value",
    "DistinguishedFolderId",
    "FieldURI",
    "ExtendedFieldURI",
    "TimeZoneDefinition",
    "RequestServerVersion",
    "ItemId",
    "FolderId",
    "CalendarView",
    "FindItem",
    "UpdateItem",
    "DeleteItem",
    "CreateItem",
    "Content",
]
ATTRIBUTE = [
    "Id",
    "ChangeKey",
    "FieldURI",
    "Traversal",
    "ConflictResolution",
    "MessageDisposition",
    "DeleteType",
    "BodyType",
    "PropertyTag",
    "PropertySetId",
    "DistinguishedPropertySetId",
    "PropertyType",
    "PropertyId",
    "PropertyName",
    "Version",
    "StartDate",
    "EndDate",
    "MaxEntriesReturned",
    "Offset",
    "Name",
    "Value",
    "RecurringMasterId",
    "SendMeetingInvitations",
    "SendMeetingCancellations",
    "AffectedTaskOccurrences",
    "SendMeetingInvitationsOrCancellations",
    "Timeout",
]


def random_tree(rnd, depth=0):
    ns = rnd.choice([MESSAGES_NS, TYPES_NS])
    e = etree.Element(f"{{{ns}}}{rnd.choice(NAMES)}", nsmap={"m": MESSAGES_NS, "t": TYPES_NS})
    for name in rnd.sample(ATTRIBUTE, rnd.randint(0, 5)):
        e.set(name, f"SECRET{rnd.randint(0, 10**6)} {rnd.choice(['a b', 'x@y.de', 'ü ö'])}")
    children = rnd.randint(0, 3) if depth < 4 else 0
    if children:
        for _ in range(children):
            e.append(random_tree(rnd, depth + 1))
    else:
        e.text = f"SECRET{rnd.randint(0, 10**6)} {rnd.choice(['value', 'a@b.de', 'zwei Wörter'])}"
    return e


def test_fuzz_random_texts_in_all_elements_and_attributes():
    rnd = random.Random(20261101)
    for _ in range(300):
        op = etree.Element(f"{{{MESSAGES_NS}}}FuzzOp", nsmap={"m": MESSAGES_NS, "t": TYPES_NS})
        for _ in range(rnd.randint(1, 4)):
            op.append(random_tree(rnd))
        body = (f"<s:Envelope {HEADER}><s:Body>" + etree.tostring(op).decode() + "</s:Body></s:Envelope>").encode()
        header = {"User-Agent": "SECRET1 a b", "SOAPAction": "SECRET2 x@y.de", "Content-Type": "SECRET3 q", "X-Test": "SECRET4"}
        s = protocol_capture.skeleton(soap.read(body, None), header, NOW)
        assert "SECRET" not in serialized(s)


# ---------------------------------------------------------------- storage


class FakeClock:
    def __init__(self, t=1_793_000_000.0):
        self.t = t

    def now(self):
        return self.t


class CollectionStorage:
    def __init__(self):
        self.entries = []

    def write(self, path, text, *, one_time=False):
        self.entries.append((Path(path), text, one_time))


def account(test=True, measure=True):
    return SimpleNamespace(name="test-user", test=test, capture=measure)


def test_only_test_accounts_with_capture_write():
    a, clock = CollectionStorage(), FakeClock()
    m = protocol_capture.ProtocolCapture(Path("/logs/capture"), clock, a)
    s = protocol_capture.skeleton(create_item(), {}, NOW)
    for k in (account(False, True), account(True, False), account(False, False)):
        asyncio.run(m.capture(k, s, ["NoError"], 5))
    assert a.entries == []
    asyncio.run(m.capture(account(), s, ["NoError"], 5))
    assert len(a.entries) == 2


def test_line_skeleton_file_and_gap():
    a, clock = CollectionStorage(), FakeClock()
    m = protocol_capture.ProtocolCapture(Path("/logs/capture"), clock, a)
    s = protocol_capture.skeleton(create_item(), {}, NOW)
    asyncio.run(m.capture(account(), s, ["NoError", "Bad Code!"], 12))
    clock.t += 187.5
    asyncio.run(m.capture(account(), s, ["ErrorServerBusy"], 7))
    lines = [e for e in a.entries if not e[2]]
    skeletons = [e for e in a.entries if e[2]]
    assert len(skeletons) == 1 and skeletons[0][0] == Path("/logs/capture/skeletons") / f"{s.fingerprint}.json"
    z1, z2 = json.loads(lines[0][1]), json.loads(lines[1][1])
    assert z1["codes"] == ["NoError"] and z1["gap_s"] is None and z1["duration_ms"] == 12
    assert z2["gap_s"] == 187.5 and z2["operation"] == "CreateItem" and z2["fingerprint"] == s.fingerprint
    assert lines[0][0].name.endswith(".jsonl") and lines[0][0].parent == Path("/logs/capture")
    for g in SECRETS:
        assert all(g not in e[1] for e in a.entries)
