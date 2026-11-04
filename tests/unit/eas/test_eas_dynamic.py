"""EAS for pattern-mode users: discovered calendars appear as folders, the dynamic EAS switch and the default-calendar rule apply."""

from tests.unit.eas.conftest import k

from davbridge.eas import tags, wbxml

BOB_PW = "pw-bob-1234"
PATTERN = {"auth": {"mode": "pattern", "username_pattern": "^[a-z][a-z0-9-]{0,30}$"}}


def dynamic(eas: bool, default: str = "personal") -> dict:
    return {**PATTERN, "dynamic_users": {"calendar_mode": "auto", "default_calendar": default, "eas_enabled": eas}}


def prepare(fake):
    fake.passwords["bob"] = BOB_PW
    fake.cal("bob", "personal").display_name = "Personal"
    fake.cal("bob", "work").display_name = "Work"


async def folder_sync(client):
    request = wbxml.encode(k("FolderHierarchy:FolderSync", k("FolderHierarchy:SyncKey", text=0)))
    return await client.raw("FolderSync", request, user="bob", password=BOB_PW)


async def test_folder_sync_lists_the_discovered_calendars(eas_bridge_with, fake):
    prepare(fake)
    async with eas_bridge_with(dynamic(True)) as client:
        response = await folder_sync(client)
        assert response.status_code == 200
        root = wbxml.decode(response.content)
        names = [tags.text_from(n, "FolderHierarchy:DisplayName") for n in tags.find_all(tags.find(root, "FolderHierarchy:Changes"), "FolderHierarchy:Add")]
        assert "Personal" in names and "Work" in names
        ids = {tags.text_from(n, "FolderHierarchy:ServerId") for n in tags.find_all(tags.find(root, "FolderHierarchy:Changes"), "FolderHierarchy:Add")}
        assert client.d.ids.calendar_key("bob", "personal") in ids and client.d.ids.calendar_key("bob", "work") in ids


async def test_dynamic_users_need_their_own_eas_switch(eas_bridge_with, fake):
    prepare(fake)
    async with eas_bridge_with(dynamic(False)) as client:
        response = await folder_sync(client)
        assert response.status_code == 403


async def test_eas_answers_403_when_the_default_calendar_is_ambiguous(eas_bridge_with, fake):
    prepare(fake)
    async with eas_bridge_with(dynamic(True, "only")) as client:
        response = await folder_sync(client)
        assert response.status_code == 403 and "WWW-Authenticate" not in response.headers
