"""Pattern mode and calendar_mode = "auto": who gets in, which calendars they see, which one is the default."""

import httpx
import pytest
from tests import soap_helpers as h
from tests.doubles.fake_nextcloud import FakeNextcloud
from tests.unit.conftest import KEY, PASSWORD, USER, config_for

from davbridge.auth import LoginRejected
from davbridge.discovery import CACHE_SECONDS, FAILURE_SECONDS
from davbridge.main import build_services, create_app
from davbridge.operations import import_all

PATTERN = {"auth": {"mode": "pattern", "username_pattern": "^[a-z][a-z0-9-]{0,30}$"}, "dynamic_users": {"calendar_mode": "auto", "default_calendar": "personal"}}
BOB = "bob"
BOB_PASSWORD = "pw-bob-1234"


def header(user: str, password: str) -> str:
    return h.basic(user, password)["Authorization"]


@pytest.fixture
def server(fake):
    return fake


@pytest.fixture
def fake() -> FakeNextcloud:
    f = FakeNextcloud({USER: PASSWORD, BOB: BOB_PASSWORD, "eve": "pw-eve-1234"})
    f.cal(USER, "test-calendar")
    f.cal(BOB, "personal").display_name = "Personal"
    f.cal(BOB, "work").display_name = "Work"
    f.cal(BOB, "contact_birthdays", writable=False).display_name = "Birthdays"
    shared = f.cal(BOB, "team_shared_by_carol", writable=False)
    shared.shared, shared.owner, shared.display_name = True, "carol", "Team"
    return f


async def login(d, user=BOB, password=BOB_PASSWORD, ip="10.0.0.1"):
    return await d.login.check(header(user, password), ip)


async def build(services_factory, sections=None):
    return await services_factory(sections if sections is not None else PATTERN)


@pytest.fixture
async def dyn(services_factory, server):
    return await build(services_factory)


async def test_pattern_user_gets_the_discovered_calendars_with_a_derived_default(dyn, server):
    s = await login(dyn)
    account = s.account
    assert account.name == BOB and account.auto and account.address == "bob@dav.example.com"
    assert [c.uri for c in account.calendar] == ["contact_birthdays", "personal", "work"]
    default = account.default_calendar()
    assert default.uri == "personal" and default.key == dyn.ids.calendar_key(BOB, "personal") and default.key.startswith("c-")
    by_uri = {c.uri: c for c in account.calendar}
    assert (by_uri["personal"].write, by_uri["contact_birthdays"].write) == ("caldav", "never")
    assert by_uri["personal"].owner == BOB and by_uri["personal"].display_name == "Personal"
    assert server.access_calls == 1 and server.discover_calls == 1


async def test_users_with_an_explicit_calendar_list_never_trigger_discovery(dyn, server):
    s = await login(dyn, USER, PASSWORD)
    assert s.account.auto is False and [c.key for c in s.account.calendar] == ["test-calendar", "read-only-calendar"]
    assert server.discover_calls == 0


async def test_user_outside_the_pattern_never_reaches_caldav(dyn, server):
    for name in ("Bob", "1bob", "bob/../x", "b%2fx", "evil@other.example.com", "x" * 80):
        with pytest.raises(LoginRejected) as e:
            await dyn.login.check(header(name, "pw"), "10.0.0.1")
        assert e.value.reason == "unknown-user"
    assert server.access_calls == 0 and server.discover_calls == 0


async def test_wrong_password_of_a_matching_user_does_not_discover_anything(dyn, server):
    with pytest.raises(LoginRejected) as e:
        await login(dyn, BOB, "wrong-password-1")
    assert e.value.reason == "wrong-password"
    assert server.access_calls == 1 and server.discover_calls == 0


async def test_login_by_address_works_in_pattern_mode(dyn, server):
    s = await login(dyn, "bob@dav.example.com")
    assert s.account.name == BOB and s.credentials.user == BOB


async def test_discovery_is_cached_for_five_minutes_and_refreshed_afterwards(dyn, server, clock):
    await login(dyn)
    await login(dyn)
    assert server.discover_calls == 1
    clock.before(CACHE_SECONDS - 1)
    await login(dyn)
    assert server.discover_calls == 1
    clock.before(2)
    server.cal(BOB, "later").display_name = "Later"
    s = await login(dyn)
    assert server.discover_calls == 2 and "later" in [c.uri for c in s.account.calendar]


async def test_calendar_ids_are_stable_and_bound_to_user_and_path_not_to_the_name(dyn, server, clock):
    first = {c.uri: c.key for c in (await login(dyn)).account.calendar}
    server.cal(BOB, "personal").display_name = "Renamed"
    clock.before(CACHE_SECONDS + 1)
    again = (await login(dyn)).account
    assert {c.uri: c.key for c in again.calendar} == first
    assert {c.uri: c.display_name for c in again.calendar}["personal"] == "Renamed"
    assert len(set(first.values())) == 3
    assert dyn.ids.calendar_key("eve", "personal") != first["personal"]
    server.calendar[(BOB, "work-2")] = server.calendar.pop((BOB, "work"))
    clock.before(CACHE_SECONDS + 1)
    moved = {c.uri: c.key for c in (await login(dyn)).account.calendar}
    assert moved["work-2"] != first["work"] and moved["personal"] == first["personal"]


async def test_shared_calendars_only_with_opt_in_and_never_writable_by_owner_rules(services_factory, server):
    d = await build(services_factory, {**PATTERN, "dynamic_users": {**PATTERN["dynamic_users"], "include_shared": True}, "features": {"read_only_owners": ["carol"]}})
    server.cal(BOB, "team-rw_shared_by_carol").shared = True
    server.calendar[(BOB, "team-rw_shared_by_carol")].owner = "carol"
    account = (await login(d)).account
    by_uri = {c.uri: c for c in account.calendar}
    assert {"team_shared_by_carol", "team-rw_shared_by_carol"} <= set(by_uri)
    assert by_uri["team-rw_shared_by_carol"].write == "never" and by_uri["team_shared_by_carol"].write == "never"
    assert by_uri["team-rw_shared_by_carol"].owner == "carol"


async def test_shared_calendars_are_excluded_by_default(dyn):
    assert "team_shared_by_carol" not in [c.uri for c in (await login(dyn)).account.calendar]


@pytest.mark.parametrize(
    ("rule", "expected"),
    [("personal", "personal"), ("work", "work"), ("first-writable", "personal"), ("c-ignored", None), ("missing", None), ("only", None)],
)
async def test_default_calendar_is_never_guessed(services_factory, server, rule, expected):
    d = await build(services_factory, {**PATTERN, "dynamic_users": {"calendar_mode": "auto", "default_calendar": rule if rule != "c-ignored" else "c-" + "0" * 24}})
    if expected is None:
        with pytest.raises(LoginRejected) as e:
            await login(d)
        assert e.value.reason == "no-default-calendar"
    else:
        assert (await login(d)).account.default_calendar().uri == expected


async def test_default_calendar_can_be_named_by_its_derived_id(services_factory, server):
    probe = await build(services_factory)
    key = probe.ids.calendar_key(BOB, "work")
    d = await build(services_factory, {**PATTERN, "dynamic_users": {"calendar_mode": "auto", "default_calendar": key}})
    assert (await login(d)).account.default_calendar().uri == "work"


async def test_only_rule_accepts_exactly_one_calendar(services_factory, server):
    d = await build(services_factory, {**PATTERN, "dynamic_users": {"calendar_mode": "auto", "default_calendar": "only"}})
    del server.calendar[(BOB, "work")], server.calendar[(BOB, "contact_birthdays")]
    assert (await login(d)).account.default_calendar().uri == "personal"


async def test_first_writable_skips_read_only_and_operator_blocked_calendars(services_factory, server):
    d = await build(services_factory, {**PATTERN, "dynamic_users": {"calendar_mode": "auto", "default_calendar": "first-writable"}})
    server.calendar[(BOB, "personal")].writable = False
    assert (await login(d)).account.default_calendar().uri == "work"


async def test_user_without_any_readable_calendar_is_refused(dyn, server):
    for key in [k for k in server.calendar if k[0] == "eve"]:
        del server.calendar[key]
    with pytest.raises(LoginRejected) as e:
        await login(dyn, "eve", "pw-eve-1234")
    assert e.value.reason == "no-default-calendar"


async def test_failed_discovery_is_not_repeated_on_every_request(services_factory, server, clock):
    d = await build(services_factory, {**PATTERN, "dynamic_users": {"calendar_mode": "auto", "default_calendar": "only"}})
    for _ in range(3):
        with pytest.raises(LoginRejected):
            await login(d)
    assert server.discover_calls == 1
    clock.before(FAILURE_SECONDS + 1)
    with pytest.raises(LoginRejected):
        await login(d)
    assert server.discover_calls == 2


async def test_caldav_failure_during_discovery_is_unavailable_not_unauthorized(dyn, server):
    from davbridge.caldav import CaldavError, CaldavKind

    await login(dyn)
    dyn.login.forget(BOB)
    dyn.login._positive.clear()
    server.failure = CaldavError(CaldavKind.BUSY, 503)
    with pytest.raises(LoginRejected) as e:
        await login(dyn)
    assert e.value.reason == "caldav-unavailable"


async def test_allowlisted_user_without_users_block_uses_discovery(services_factory, server):
    d = await build(services_factory, {"auth": {"allowed_users": [USER, BOB]}, "dynamic_users": {"calendar_mode": "auto", "default_calendar": "personal"}})
    s = await login(d)
    assert s.account.auto and s.account.default_calendar().uri == "personal"
    with pytest.raises(LoginRejected) as e:
        await login(d, "eve", "pw-eve-1234")
    assert e.value.reason == "unknown-user"
    assert server.discover_calls == 1


async def test_forgetting_an_account_drops_its_discovery_cache(dyn, server):
    await login(dyn)
    dyn.login.forget(BOB)
    dyn.login._positive.clear()
    await login(dyn)
    assert server.discover_calls == 2


@pytest.fixture
async def dynamic_bridge(server, clock, tmp_path):
    import_all()

    async def factory():
        d = await build_services(config_for(tmp_path, PATTERN), KEY, clock, caldav=server)
        d.notifier.watch = lambda account: None
        return d

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client


def find_folder() -> bytes:
    return h.envelope(
        '<m:FindFolder Traversal="Deep"><m:FolderShape><t:BaseShape>AllProperties</t:BaseShape></m:FolderShape><m:ParentFolderIds><t:DistinguishedFolderId Id="msgfolderroot"/></m:ParentFolderIds></m:FindFolder>'
    )


async def test_ews_lists_the_discovered_calendars(dynamic_bridge):
    r = await dynamic_bridge.post("/EWS/Exchange.asmx", content=find_folder(), headers=h.basic(BOB, BOB_PASSWORD))
    assert r.status_code == 200
    names = [e.text for e in h.parse(r.content).iter("{%s}DisplayName" % h.T)]
    assert "Personal" in names and "Work" in names and "Birthdays" in names


async def test_ews_answers_403_when_no_default_calendar_can_be_chosen(services_factory, server, clock, tmp_path):
    import_all()
    sections = {**PATTERN, "dynamic_users": {"calendar_mode": "auto", "default_calendar": "only"}}

    async def factory():
        d = await build_services(config_for(tmp_path, sections), KEY, clock, caldav=server)
        d.notifier.watch = lambda account: None
        return d

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/EWS/Exchange.asmx", content=find_folder(), headers=h.basic(BOB, BOB_PASSWORD))
            assert r.status_code == 403 and "WWW-Authenticate" not in r.headers


async def test_ews_unknown_pattern_user_gets_401_without_a_caldav_request(dynamic_bridge, server):
    r = await dynamic_bridge.post("/EWS/Exchange.asmx", content=find_folder(), headers=h.basic("Not-Allowed", "pw"))
    assert r.status_code == 401 and server.access_calls == 0 and server.discover_calls == 0


MIXED_CASE = {"auth": {"mode": "pattern", "username_pattern": "^[A-Za-z][A-Za-z0-9-]{0,30}$"}, "dynamic_users": {"calendar_mode": "auto", "default_calendar": "personal"}}


async def test_pattern_mode_maps_every_spelling_of_a_login_to_the_server_user_id(services_factory, server, clock):
    server.canonical_ids["bob"] = BOB
    d = await build(services_factory, MIXED_CASE)
    spellings = ["Bob", "BOB", "bob", "Bob@dav.example.com", "BOB@DAV.EXAMPLE.COM"]
    sessions = [await login(d, name) for name in spellings]
    assert {s.account.name for s in sessions} == {BOB} and {s.credentials.user for s in sessions} == {BOB}
    assert {s.account.address for s in sessions} == {"bob@dav.example.com"}
    assert {c.key for s in sessions for c in s.account.calendar if c.uri == "personal"} == {d.ids.calendar_key(BOB, "personal")}
    assert server.canonical_calls == 1 and server.discover_calls == 1


async def test_the_server_spelling_wins_even_if_it_has_capital_letters(services_factory, server):
    server.canonical_ids["carol"] = "Carol"
    server.passwords["Carol"] = "pw-carol-1234"
    server.cal("Carol", "personal")
    d = await build(services_factory, MIXED_CASE)
    first = await login(d, "carol", "pw-carol-1234")
    second = await login(d, "CAROL", "pw-carol-1234")
    assert first.account.name == second.account.name == "Carol" and first.credentials.user == "Carol"
    assert second.account.calendar[0].key == d.ids.calendar_key("Carol", "personal")


async def test_wrong_password_with_another_spelling_stays_rejected_and_cached(services_factory, server):
    server.canonical_ids["bob"] = BOB
    d = await build(services_factory, MIXED_CASE)
    await login(d, BOB)
    for name in ("Bob", "BOB"):
        with pytest.raises(LoginRejected) as e:
            await login(d, name, "wrong-password-1")
        assert e.value.reason in ("wrong-password", "wrong-password-cached")


async def test_explicit_users_keep_their_configured_spelling(services_factory, server):
    server.canonical_ids[USER.lower()] = USER
    d = await build(services_factory, MIXED_CASE)
    s = await login(d, USER.upper(), PASSWORD)
    assert s.account.name == USER and server.canonical_calls == 0
