"""GetUserOofSettings: truthfully disabled for the own mailbox, no information about other mailboxes."""

from tests import soap_helpers as h
from tests.unit.conftest import USER, ops, post

NS = h.NS


def request(address: str | None = None) -> str:
    mailbox = f"<t:Mailbox><t:Address>{address}</t:Address></t:Mailbox>" if address else ""
    return f"<m:GetUserOofSettingsRequest>{mailbox}</m:GetUserOofSettingsRequest>"


async def test_oof_is_disabled_for_the_own_mailbox(bridge):
    client, _ = bridge
    root = await post(client, request("test-user@dav.example.com"))
    response = root.find("s:Body/m:GetUserOofSettingsResponse", NS)
    assert response is not None
    message = response.find("m:ResponseMessage", NS)
    assert message.get("ResponseClass") == "Success" and message.findtext("m:ResponseCode", namespaces=NS) == "NoError"
    assert response.findtext("t:OofSettings/t:OofState", namespaces=NS) == "Disabled"
    assert response.findtext("t:OofSettings/t:ExternalAudience", namespaces=NS) == "None"
    assert response.findtext("t:OofSettings/t:InternalReply/t:Message", "", namespaces=NS) == ""
    assert response.findtext("m:AllowExternalOof", namespaces=NS) == "None"


async def test_oof_without_mailbox_element_and_with_the_user_name(bridge):
    client, _ = bridge
    for payload in (request(), request(USER.upper())):
        assert h.codes(await post(client, payload)) == ["NoError"]


async def test_oof_of_another_mailbox_is_not_answered(bridge):
    client, _ = bridge
    root = await post(client, request("someone-else@dav.example.com"))
    assert h.codes(root) == ["ErrorNonExistentMailbox"]
    assert root.find(".//t:OofState", NS) is None


async def test_oof_handler_answers_without_a_caldav_request(services, fake):
    before = fake.access_calls
    result = await ops(services, request())
    assert result.codes == ("NoError",) and fake.access_calls == before
