"""Calendar autodiscovery in the CalDAV client against synthetic Sabre-style responses (principal, calendar home, boundaries)."""

import re
from pathlib import Path

import httpx
import pytest

from davbridge.caldav import CaldavError, CaldavKind, Credentials, HttpCaldav
from davbridge.config import CalendarConfig

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "caldav"
BASE = "https://cloud.example.com/remote.php/dav"
Z = Credentials("test-user", "pw-test-1234")
PRINCIPAL_URL = "/remote.php/dav/principals/users/test-user/"
HOME_URL = "/remote.php/dav/calendars/test-user/"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class Server:
    """Answers the two discovery requests; `principal` and `home` can be replaced to model hostile or broken servers."""

    def __init__(self, principal: str | None = None, home: str | None = None, **overrides) -> None:
        self.principal = principal if principal is not None else fixture("principal.xml")
        self.home = home if home is not None else fixture("home.xml")
        self.requests: list[httpx.Request] = []
        self.overrides = overrides

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "PROPFIND"
        override = self.overrides.get(request.url.path)
        if override is not None:
            return override
        if request.url.path == PRINCIPAL_URL:
            return httpx.Response(207, content=self.principal.encode())
        if request.url.path == HOME_URL:
            assert request.headers["Depth"] == "1"
            return httpx.Response(207, content=self.home.encode())
        return httpx.Response(404)


def caldav_for(server: Server, **client_options) -> HttpCaldav:
    return HttpCaldav(BASE, httpx.AsyncClient(transport=httpx.MockTransport(server), **client_options))


async def test_discovery_lists_own_readable_vevent_calendars_with_rights():
    server = Server()
    found = await caldav_for(server).discover_calendars(Z, include_shared=False)
    by_uri = {c.uri: c for c in found}
    assert list(by_uri) == ["contact_birthdays", "flagged", "personal", "work"]
    assert (by_uri["personal"].display_name, by_uri["personal"].writable, by_uri["personal"].shared, by_uri["personal"].owner) == ("Personal", True, False, "test-user")
    assert by_uri["contact_birthdays"].writable is False
    assert by_uri["flagged"].writable is False
    assert len(server.requests) == 2


async def test_discovery_excludes_todo_only_unreadable_inbox_trash_and_subscriptions():
    found = await caldav_for(Server()).discover_calendars(Z, include_shared=True)
    uris = {c.uri for c in found}
    assert not uris & {"tasks-only", "hidden", "inbox", "trashbin", "subscription"}


async def test_shared_calendars_are_opt_in_and_keep_their_rights():
    found = await caldav_for(Server()).discover_calendars(Z, include_shared=True)
    by_uri = {c.uri: c for c in found}
    assert {"team_shared_by_bob", "team-rw_shared_by_carol"} <= set(by_uri)
    assert by_uri["team_shared_by_bob"].shared and by_uri["team_shared_by_bob"].owner == "bob" and by_uri["team_shared_by_bob"].writable is False
    assert by_uri["team-rw_shared_by_carol"].shared and by_uri["team-rw_shared_by_carol"].owner == "carol" and by_uri["team-rw_shared_by_carol"].writable is True
    assert by_uri["personal"].shared is False


async def test_calendar_without_a_verifiable_owner_is_not_own():
    home = fixture("home.xml").replace("<d:owner><d:href>/remote.php/dav/principals/users/test-user/</d:href></d:owner>", "")
    assert await caldav_for(Server(home=home)).discover_calendars(Z, include_shared=False) == []
    assert {c.uri for c in await caldav_for(Server(home=home)).discover_calendars(Z, include_shared=True)} >= {"personal", "work"}


async def test_owner_principal_property_is_the_fallback_for_the_owner():
    home = fixture("home.xml").replace("<d:owner><d:href>/remote.php/dav/principals/users/test-user/</d:href></d:owner>", "<oc:owner-principal>principals/users/test-user</oc:owner-principal>")
    found = await caldav_for(Server(home=home)).discover_calendars(Z, include_shared=False)
    assert {c.uri for c in found} == {"contact_birthdays", "flagged", "personal", "work"}


async def test_missing_privilege_set_means_no_read_right_and_no_calendar():
    home = re.sub(r"<d:current-user-privilege-set>.*?</d:current-user-privilege-set>", "", fixture("home.xml"), flags=re.S)
    assert "current-user-privilege-set" not in home
    assert await caldav_for(Server(home=home)).discover_calendars(Z, include_shared=True) == []


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example.org/remote.php/dav/calendars/test-user/personal/",
        "http://cloud.example.com/remote.php/dav/calendars/test-user/personal/",
        "https://cloud.example.com:8443/remote.php/dav/calendars/test-user/personal/",
        "/remote.php/dav/calendars/test-user/../other/personal/",
        "/remote.php/dav/calendars/test-user/%252e%252e/personal/",
        "/remote.php/dav/calendars/test-user/%2e%2e/other/",
        "/remote.php/dav/calendars/other-user/personal/",
        "/remote.php/dav/calendars/test-user/sub/dir/",
        "/remote.php/dav/calendars/test-user/personal/?x=1",
        "/remote.php/dav/calendars/test-user//personal/",
        "remote.php/dav/calendars/test-user/personal/",
        "/remote.php/dav/calendars/test-user/pers%5Conal/",
    ],
)
async def test_hrefs_outside_the_calendar_home_are_ignored(href):
    home = fixture("home.xml").replace("/remote.php/dav/calendars/test-user/personal/", href)
    caldav = caldav_for(Server(home=home))
    found = await caldav.discover_calendars(Z, include_shared=False)
    assert "personal" not in {c.uri for c in found} and {c.uri for c in found} == {"contact_birthdays", "flagged", "work"}
    assert caldav.foreign_paths >= 1


async def test_percent_encoded_segment_is_decoded_exactly_once():
    home = fixture("home.xml").replace("/calendars/test-user/work/", "/calendars/test-user/my%40cal/")
    found = await caldav_for(Server(home=home)).discover_calendars(Z, include_shared=False)
    assert "my@cal" in {c.uri for c in found}


@pytest.mark.parametrize(
    ("find", "replace"),
    [
        ("<c:calendar-home-set><d:href>/remote.php/dav/calendars/test-user/</d:href>", "<c:calendar-home-set><d:href>/remote.php/dav/calendars/another/</d:href>"),
        ("<c:calendar-home-set><d:href>/remote.php/dav/calendars/test-user/</d:href>", "<c:calendar-home-set><d:href>https://evil.example.org/remote.php/dav/calendars/test-user/</d:href>"),
        ("<c:calendar-home-set><d:href>/remote.php/dav/calendars/test-user/</d:href>", "<c:calendar-home-set><d:href>/remote.php/dav/calendars/test-user/../x/</d:href>"),
        ("<d:current-user-principal><d:href>/remote.php/dav/principals/users/test-user/", "<d:current-user-principal><d:href>/remote.php/dav/principals/users/other/"),
        ("<c:calendar-home-set><d:href>/remote.php/dav/calendars/test-user/</d:href></c:calendar-home-set>", ""),
    ],
)
async def test_principal_or_home_that_is_not_the_authenticated_users_is_refused(find, replace):
    server = Server(principal=fixture("principal.xml").replace(find, replace))
    with pytest.raises(CaldavError) as e:
        await caldav_for(server).discover_calendars(Z, include_shared=False)
    assert e.value.kind == CaldavKind.UNEXPECTED
    assert len(server.requests) == 1


@pytest.mark.parametrize("status", [301, 302, 307, 308])
@pytest.mark.parametrize("follow", [False, True])
async def test_redirects_are_refused_even_if_the_client_would_follow_them(status, follow):
    other = httpx.Response(207, content=fixture("principal.xml").encode())
    redirect = httpx.Response(status, headers={"Location": "https://cloud.example.com/remote.php/dav/principals/users/elsewhere/"})
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return redirect if request.url.path == PRINCIPAL_URL else other

    caldav = HttpCaldav(BASE, httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=follow))
    with pytest.raises(CaldavError) as e:
        await caldav.discover_calendars(Z, include_shared=False)
    assert e.value.kind == CaldavKind.UNEXPECTED
    assert len(calls) <= 2 and calls[0] == PRINCIPAL_URL


async def test_server_errors_map_to_caldav_errors():
    server = Server(**{PRINCIPAL_URL: httpx.Response(401)})
    with pytest.raises(CaldavError) as e:
        await caldav_for(server).discover_calendars(Z, include_shared=False)
    assert e.value.kind == CaldavKind.AUTH
    server = Server(**{HOME_URL: httpx.Response(503)})
    with pytest.raises(CaldavError) as e:
        await caldav_for(server).discover_calendars(Z, include_shared=False)
    assert e.value.kind == CaldavKind.BUSY


async def test_user_names_are_percent_encoded_in_the_discovery_urls():
    seen = []

    def handler(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(404)

    caldav = HttpCaldav(BASE, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(CaldavError):
        await caldav.discover_calendars(Credentials("a/b%2f..", "pw"), include_shared=False)
    assert seen == ["/remote.php/dav/principals/users/a%2Fb%252f../"]


SUB_BASE = "https://cloud.example.com/nextcloud/remote.php/dav"
SUB_PRINCIPAL_URL = "/nextcloud" + PRINCIPAL_URL
SUB_HOME_URL = "/nextcloud" + HOME_URL


def sub_path_caldav(principal: str | None = None) -> tuple[HttpCaldav, list[str]]:
    """The synthetic server below the web root `/nextcloud`: every href and every request path carries the prefix."""
    seen: list[str] = []
    principal = principal if principal is not None else fixture("principal.xml").replace("/remote.php/", "/nextcloud/remote.php/")
    home = fixture("home.xml").replace("/remote.php/", "/nextcloud/remote.php/")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == SUB_PRINCIPAL_URL:
            return httpx.Response(207, content=principal.encode())
        if request.url.path == SUB_HOME_URL:
            return httpx.Response(207, content=home.encode())
        return httpx.Response(404)

    return HttpCaldav(SUB_BASE, httpx.AsyncClient(transport=httpx.MockTransport(handler))), seen


async def test_discovery_works_for_a_server_in_a_sub_path():
    caldav, seen = sub_path_caldav()
    found = await caldav.discover_calendars(Z, include_shared=False)
    assert [c.uri for c in found] == ["contact_birthdays", "flagged", "personal", "work"]
    assert caldav.foreign_paths == 0
    assert seen == [SUB_PRINCIPAL_URL, SUB_HOME_URL]


async def test_sub_path_discovery_still_refuses_a_principal_outside_the_prefix():
    caldav, _ = sub_path_caldav(principal=fixture("principal.xml"))
    with pytest.raises(CaldavError) as e:
        await caldav.discover_calendars(Z, include_shared=False)
    assert e.value.kind == CaldavKind.UNEXPECTED


async def test_calendar_requests_and_hrefs_keep_the_sub_path_prefix():
    seen: list[tuple[str, bytes]] = []
    prefix = "/nextcloud/remote.php/dav/calendars/test-user/personal/"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.content))
        body = (
            '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>' + prefix + 'a.ics</d:href><d:propstat><d:prop><d:getetag>"1"</d:getetag>'
            "<c:calendar-data>BEGIN:VCALENDAR</c:calendar-data></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            "<d:sync-token>http://sabre.io/ns/sync/3</d:sync-token></d:multistatus>"
        )
        return httpx.Response(207, content=body.encode())

    caldav = HttpCaldav(SUB_BASE, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    cal = CalendarConfig("personal", "personal", "Personal", True, "caldav", "test-user")
    delta = await caldav.sync(Z, cal, None)
    assert list(delta.changed) == ["a.ics"] and caldav.foreign_paths == 0
    objects = await caldav.get_several(Z, cal, ["a.ics"])
    assert [o.href for o in objects] == ["a.ics"]
    assert seen[0][0] == prefix and seen[1][0] == prefix
    assert (prefix + "a.ics").encode() in seen[1][1]


def principal_answering(href: str) -> HttpCaldav:
    body = f'<d:multistatus xmlns:d="DAV:"><d:response><d:href>{PRINCIPAL_URL}</d:href><d:propstat><d:prop><d:current-user-principal><d:href>{href}</d:href></d:current-user-principal></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'
    return HttpCaldav(BASE, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(207, content=body.encode()))))


@pytest.mark.parametrize(
    ("typed", "href", "expected"),
    [
        ("Test-User", "/remote.php/dav/principals/users/test-user/", "test-user"),
        ("test-user", "/remote.php/dav/principals/users/Test-User/", "Test-User"),
        ("test-user", "/remote.php/dav/principals/users/test-user/", "test-user"),
        ("a@x.org", "/remote.php/dav/principals/users/A%40x.org/", "A@x.org"),
        ("test-user", "/remote.php/dav/principals/users/other-user/", None),
        ("test-user", "/remote.php/dav/principals/users/test-user/sub/", None),
        ("test-user", "/remote.php/dav/principals/groups/test-user/", None),
        ("test-user", "https://evil.example.org/remote.php/dav/principals/users/test-user/", None),
        ("test-user", "/remote.php/dav/principals/users/test-user/../x/", None),
        ("test-user", "/remote.php/dav/principals/users/test%252duser/", None),
    ],
)
async def test_canonical_user_is_the_servers_spelling_only_if_it_differs_by_case(typed, href, expected):
    assert await principal_answering(href).canonical_user(Credentials(typed, "pw")) == expected


@pytest.mark.parametrize("segment", ["x%2F..%2F..%2Fbob%2Fcalendar", "a%2fb", "a%5Cb", "x%2Ex", "x%2ex", "x%252F..%252Fbob", "x%252e%252e%252fsecret"])
async def test_discovered_calendars_with_encoded_separators_are_ignored(segment):
    # A single decoding of the href never yields a separator or a dot segment that survives: the segment stays one plain name or the entry is dropped.
    home = fixture("home.xml").replace("/calendars/test-user/work/", f"/calendars/test-user/{segment}/")
    caldav = caldav_for(Server(home=home))
    found = await caldav.discover_calendars(Z, include_shared=False)
    assert all("%" not in c.uri and "%2" not in c.uri.lower() and "%5c" not in c.uri.lower() for c in found) and "work" not in {c.uri for c in found}
