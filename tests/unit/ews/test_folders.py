from tests import soap_helpers as h
from tests.unit.conftest import USER, codes, messages_xml, ops

NS = h.NS


def find_folder(parent: str, deep: bool = False, folder_id: bool = False) -> str:
    target = f'<t:FolderId Id="{parent}"/>' if folder_id else f'<t:DistinguishedFolderId Id="{parent}"/>'
    return (
        f'<m:FindFolder Traversal="{"Deep" if deep else "Shallow"}"><m:FolderShape><t:BaseShape>AllProperties</t:BaseShape></m:FolderShape>'
        f"<m:ParentFolderIds>{target}</m:ParentFolderIds></m:FindFolder>"
    )


def names(message) -> list[str]:
    return [e.text for e in message.iter("{%s}DisplayName" % h.T)]


async def test_get_folder_several_in_request_order_with_unknown_distinguished_id(services):
    e = await ops(services, h.get_folder("msgfolderroot", "inbox", "calendar", "archivemsgfolderroot", "contacts"))
    assert codes(e) == ["NoError", "NoError", "NoError", "ErrorFolderNotFound", "NoError"]
    assert names(messages_xml(e)[0]) == ["Top of Information Store"]
    assert names(messages_xml(e)[2]) == ["Test Calendar"]


async def test_get_folder_without_folder_is_error(services):
    e = await ops(services, "<m:GetFolder><m:FolderShape><t:BaseShape>Default</t:BaseShape></m:FolderShape><m:FolderIds/></m:GetFolder>")
    assert codes(e) == ["ErrorFolderNotFound"]


async def test_get_folder_foreign_mailbox_and_own_address(services):
    foreign = '<m:GetFolder><m:FolderIds><t:DistinguishedFolderId Id="calendar"><t:Mailbox><t:EmailAddress>jemand@other.test</t:EmailAddress></t:Mailbox></t:DistinguishedFolderId></m:FolderIds></m:GetFolder>'
    own = foreign.replace("jemand@other.test", "TEST-USER@dav.example.com")
    assert codes(await ops(services, foreign)) == ["ErrorNonExistentMailbox"]
    assert codes(await ops(services, own)) == ["NoError"]


async def test_get_folder_by_folder_id_malformed_unknown_foreign(services):
    ids = services.ids
    good = ids.folder_id(USER, "test-calendar")
    unknown = ids.folder_id(USER, "does-not-exist")
    foreign_account = ids.folder_id("other", "test-calendar")
    xml = "<m:GetFolder><m:FolderIds>" + "".join(f'<t:FolderId Id="{i}"/>' for i in (good, "broken", unknown, foreign_account)) + "</m:FolderIds></m:GetFolder>"
    assert codes(await ops(services, xml)) == ["NoError", "ErrorInvalidIdMalformed", "ErrorFolderNotFound", "ErrorInvalidIdMalformed"]


async def test_folder_permissions_from_nextcloud_and_policy(services):
    e = await ops(services, h.get_folder("calendar"))
    permissions = {x.tag.split("}")[1]: x.text for x in messages_xml(e)[0].find(".//t:EffectiveRights", NS)}
    assert permissions["CreateContents"] == "true" and permissions["Read"] == "true" and permissions["Modify"] == "false"
    read_only = services.ids.folder_id(USER, "read-only-calendar")
    e = await ops(services, f'<m:GetFolder><m:FolderIds><t:FolderId Id="{read_only}"/></m:FolderIds></m:GetFolder>')
    permissions = {x.tag.split("}")[1]: x.text for x in messages_xml(e)[0].find(".//t:EffectiveRights", NS)}
    assert permissions["CreateContents"] == "false" and permissions["Read"] == "true"


async def test_further_calendar_is_subfolder_of_the_default_calendar(services):
    read_only = services.ids.folder_id(USER, "read-only-calendar")
    e = await ops(services, f'<m:GetFolder><m:FolderIds><t:FolderId Id="{read_only}"/></m:FolderIds></m:GetFolder>')
    parents = messages_xml(e)[0].find(".//t:ParentFolderId", NS).get("Id")
    assert services.ids.read_folder_id(USER, parents) == "test-calendar"
    assert messages_xml(e)[0].find(".//t:FolderClass", NS).text == "IPF.Appointment"


async def test_find_folder_flat_and_deep(services):
    flat = await ops(services, find_folder("msgfolderroot"))
    n = names(messages_xml(flat)[0])
    assert "Inbox" in n and "Test Calendar" in n and "Read-Only Calendar" not in n and "Mailbox" not in n
    deep = await ops(services, find_folder("msgfolderroot", deep=True))
    assert "Read-Only Calendar" in names(messages_xml(deep)[0])
    under_calendar = await ops(services, find_folder("calendar"))
    assert names(messages_xml(under_calendar)[0]) == ["Read-Only Calendar"]
    root = messages_xml(under_calendar)[0].find(".//m:RootFolder", NS)
    assert root.get("TotalItemsInView") == "1" and root.get("IncludesLastItemInRange") == "true"


async def test_find_folder_unknown_parent_folder(services):
    assert codes(await ops(services, find_folder("archivemsgfolderroot"))) == ["ErrorFolderNotFound"]
    assert codes(await ops(services, "<m:FindFolder Traversal='Shallow'><m:ParentFolderIds/></m:FindFolder>")) == ["ErrorFolderNotFound"]


def sync_hierarchy(state: str | None = None) -> str:
    st = f"<m:SyncState>{state}</m:SyncState>" if state else ""
    return f"<m:SyncFolderHierarchy><m:FolderShape><t:BaseShape>AllProperties</t:BaseShape></m:FolderShape>{st}</m:SyncFolderHierarchy>"


async def test_sync_folder_hierarchy_first_create_then_stable(services):
    first = await ops(services, sync_hierarchy())
    assert codes(first) == ["NoError"]
    n = messages_xml(first)[0]
    assert len(n.findall(".//t:Create", NS)) == 16 and not n.findall(".//t:Update", NS)
    state = n.find("m:SyncState", NS).text
    second = await ops(services, sync_hierarchy(state))
    z = messages_xml(second)[0]
    assert z.find("m:SyncState", NS).text == state
    assert len(z.find("m:Changes", NS)) == 0
    assert z.find("m:IncludesLastFolderInRange", NS).text == "true"


async def test_sync_folder_hierarchy_other_state_yields_updates(services):
    old = services.ids.hierarchy_state(USER, b"alter-stand")
    e = await ops(services, sync_hierarchy(old))
    n = messages_xml(e)[0]
    assert len(n.findall(".//t:Update", NS)) == 16 and not n.findall(".//t:Create", NS)
    assert services.ids.read_hierarchy_state(USER, n.find("m:SyncState", NS).text) != b"alter-stand"


async def test_sync_folder_hierarchy_tampered_or_foreign_state(services):
    assert codes(await ops(services, sync_hierarchy("broken"))) == ["ErrorInvalidSyncStateData"]
    foreign = services.ids.hierarchy_state("other", b"x")
    assert codes(await ops(services, sync_hierarchy(foreign))) == ["ErrorInvalidSyncStateData"]


async def test_folder_over_http(bridge):
    client, d = bridge
    from tests.unit.conftest import post

    w = await post(client, h.get_folder("calendar", "inbox"))
    assert h.codes(w) == ["NoError", "NoError"]


async def test_calendar_changekey_follows_the_sync_token(services):
    from davbridge.notifier import _State

    async def ck(folder="calendar"):
        return messages_xml(await ops(services, h.get_folder(folder)))[0].find(".//t:FolderId", h.NS).get("ChangeKey")

    state = services.notifier._state.setdefault((USER, "test-calendar"), _State())
    state.token = None
    without = await ck()
    state.token = "http://sabre.io/ns/sync/7"
    seven = await ck()
    state.token = "http://sabre.io/ns/sync/8"
    eight = await ck()
    assert len({without, seven, eight}) == 3 and await ck() == eight
    assert await ck("inbox")
