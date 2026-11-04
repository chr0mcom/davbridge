import base64

import httpx
import pytest
from tests import soap_helpers as h
from tests.unit.conftest import PASSWORD, USER

from davbridge.auth import LoginRejected
from davbridge.caldav import CaldavError, CaldavKind, Credentials, HttpCaldav


def basic(user: str, password: str) -> str:
    return h.basic(user, password)["Authorization"]


async def reason(d, header, ip="10.0.0.1") -> str:
    with pytest.raises(LoginRejected) as e:
        await d.login.check(header, ip)
    return e.value.reason


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer abc",
        "Basic",
        "Basic !!!",
        "Basic " + base64.b64encode(b"ohnedoppelpunkt").decode(),
        "Basic " + base64.b64encode(b"test-user:").decode(),
        "Basic " + base64.b64encode(b":pw").decode(),
        "Basic " + base64.b64encode(b"x" * 600 + b":pw").decode(),
        "Basic " + base64.b64encode(b"\xff\xfe:\xff").decode(),
    ],
)
async def test_invalid_basic_formats(services, fake, header):
    assert await reason(services, header) == "invalid-authorization"
    assert fake.access_calls == 0


async def test_successful_login_remembers_access(services, fake):
    s = await services.login.check(basic(USER, PASSWORD), "10.0.0.1")
    assert s.account.name == USER and s.credentials.password == PASSWORD
    assert services.login.access_for(USER).password == PASSWORD
    assert fake.access_calls == 1


async def test_login_by_address_case_insensitive(services):
    s = await services.login.check(basic("TEST-USER@dav.example.com", PASSWORD), "10.0.0.1")
    assert s.account.name == USER and s.credentials.user == USER


async def test_allowlist_foreign_user_never_reaches_nextcloud(services, fake):
    fake.passwords["alice"] = "secret-1234"
    assert await reason(services, basic("alice", "secret-1234")) == "unknown-user"
    assert fake.access_calls == 0


async def test_wrong_password_then_negative_cache_without_nextcloud_and_without_counting(services_factory, fake):
    d = await services_factory(failure_limit=3)
    assert await reason(d, basic(USER, "wrong-0001")) == "wrong-password"
    assert fake.access_calls == 1
    for _ in range(10):
        assert await reason(d, basic(USER, "wrong-0001")) == "wrong-password-cached"
    assert fake.access_calls == 1
    assert await d.store.lock_check("10.0.0.1") == 0
    s = await d.login.check(basic(USER, PASSWORD), "10.0.0.1")
    assert s.account.name == USER


async def test_negative_cache_expires(services, fake, clock):
    assert await reason(services, basic(USER, "wrong-0001")) == "wrong-password"
    clock.before(901)
    assert await reason(services, basic(USER, "wrong-0001")) == "wrong-password"
    assert fake.access_calls == 2


async def test_lock_after_n_failed_attempts_and_expiry(services_factory, fake, clock):
    d = await services_factory(failure_limit=3)
    for i in range(3):
        assert await reason(d, basic(USER, f"wrong-{i:04d}")) == "wrong-password"
    calls = fake.access_calls
    assert await reason(d, basic(USER, PASSWORD)) == "locked"
    assert fake.access_calls == calls
    assert await d.store.lock_check("10.0.0.1") > 0
    assert (await d.login.check(basic(USER, PASSWORD), "10.0.0.2")).account.name == USER
    clock.before(901)
    assert (await d.login.check(basic(USER, PASSWORD), "10.0.0.1")).account.name == USER


async def test_foreign_user_and_format_error_count_towards_the_lock(services_factory):
    d = await services_factory(failure_limit=3)
    await reason(d, "Basic ???")
    await reason(d, basic("unknown", "x-1234"))
    await reason(d, "Basic !!!")
    assert await reason(d, basic(USER, PASSWORD)) == "locked"


async def test_missing_header_and_foreign_schemes_never_count(services_factory):
    d = await services_factory(failure_limit=3)
    for header in (None, "", "Bearer abc", "NTLM xyz", "Negotiate abc") * 4:
        assert await reason(d, header) == "invalid-authorization"
    assert (await d.login.check(basic(USER, PASSWORD), "10.0.0.1")).account.name == USER


async def test_cache_hit_resets_counter_and_level(services_factory):
    d = await services_factory(failure_limit=3)
    await d.login.check(basic(USER, PASSWORD), "10.0.0.1")
    await reason(d, basic(USER, "wrong-0001"))
    await reason(d, basic(USER, "wrong-0002"))
    await d.login.check(basic(USER, PASSWORD), "10.0.0.1")
    await reason(d, basic(USER, "wrong-0003"))
    await reason(d, basic(USER, "wrong-0004"))
    assert await d.store.lock_check("10.0.0.1") == 0


async def test_failed_attempts_expire_after_the_time_window(services_factory, clock):
    d = await services_factory(failure_limit=3)
    await reason(d, basic(USER, "wrong-0001"))
    await reason(d, basic(USER, "wrong-0002"))
    clock.before(1000)
    await reason(d, basic(USER, "wrong-0003"))
    assert await d.store.lock_check("10.0.0.1") == 0


async def test_success_resets_counter(services_factory):
    d = await services_factory(failure_limit=3)
    await reason(d, basic(USER, "wrong-0001"))
    await reason(d, basic(USER, "wrong-0002"))
    await d.login.check(basic(USER, PASSWORD), "10.0.0.1")
    await reason(d, basic(USER, "wrong-0003"))
    await reason(d, basic(USER, "wrong-0004"))
    assert await d.store.lock_check("10.0.0.1") == 0


async def test_positive_cache_and_recheck_after_five_minutes(services, fake, clock):
    await services.login.check(basic(USER, PASSWORD), "10.0.0.1")
    await services.login.check(basic(USER, PASSWORD), "10.0.0.1")
    assert fake.access_calls == 1
    clock.before(301)
    await services.login.check(basic(USER, PASSWORD), "10.0.0.1")
    assert fake.access_calls == 2


async def test_revoked_password_is_rejected_after_recheck(services, fake, clock):
    await services.login.check(basic(USER, PASSWORD), "10.0.0.1")
    fake.passwords[USER] = "neu-9999"
    clock.before(301)
    assert await reason(services, basic(USER, PASSWORD)) == "wrong-password"
    assert (await services.login.check(basic(USER, "neu-9999"), "10.0.0.1")).account.name == USER


async def test_nextcloud_outage_is_no_failed_attempt(services_factory, fake):
    d = await services_factory(failure_limit=3)
    fake.failure = CaldavError(CaldavKind.BUSY, 503)
    for _ in range(10):
        assert await reason(d, basic(USER, PASSWORD)) == "caldav-unavailable"
    fake.failure = CaldavError(CaldavKind.NETWORK, None)
    assert await reason(d, basic(USER, PASSWORD)) == "caldav-unavailable"
    fake.failure = None
    assert await d.store.lock_check("10.0.0.1") == 0
    assert (await d.login.check(basic(USER, PASSWORD), "10.0.0.1")).account.name == USER


async def test_valid_positive_cache_bridges_nextcloud_outage(services, fake, clock):
    await services.login.check(basic(USER, PASSWORD), "10.0.0.1")
    fake.failure = CaldavError(CaldavKind.BUSY, 503)
    clock.before(100)
    assert (await services.login.check(basic(USER, PASSWORD), "10.0.0.1")).account.name == USER
    clock.before(300)
    assert await reason(services, basic(USER, PASSWORD)) == "caldav-unavailable"


async def test_forget_removes_access_and_positive_cache(services, fake):
    await services.login.check(basic(USER, PASSWORD), "10.0.0.1")
    services.login.forget(USER)
    assert services.login.access_for(USER) is None
    await services.login.check(basic(USER, PASSWORD), "10.0.0.1")
    assert fake.access_calls == 2


async def test_clean_up_forgets_access_after_idle_except_with_live_subscription(services, clock):
    await services.login.check(basic(USER, PASSWORD), "10.0.0.1")
    clock.before(1700)
    services.login.clean_up()
    assert services.login.access_for(USER) is not None
    clock.before(200)
    services.login.clean_up(keep={USER})
    assert services.login.access_for(USER) is not None
    services.login.clean_up()
    assert services.login.access_for(USER) is None


async def test_password_not_in_repr_and_not_in_store(services, tmp_path):
    s = await services.login.check(basic(USER, PASSWORD), "10.0.0.1")
    assert PASSWORD not in repr(s.credentials) and PASSWORD not in repr(s)
    await services.store.lock_failed_attempt("9.9.9.9")
    for file in (tmp_path / "state").glob("davbridge.sqlite*"):
        assert PASSWORD.encode() not in file.read_bytes()


# ------------------------------------------------------------------ HTTP level


async def test_http_401_with_www_authenticate(bridge):
    client, d = bridge
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client._transport.app), base_url="http://test") as raw:
        for header in ({}, h.basic(USER, "wrong-0001"), h.basic("alice", "egal-1234")):
            r = await raw.post("/EWS/Exchange.asmx", content=h.envelope(h.get_folder("inbox")), headers=header)
            assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic realm=")
            assert r.content == b""


async def test_http_503_on_nextcloud_outage_without_cache(bridge, fake):
    client, d = bridge
    d.login.forget(USER)
    d.login._positive.clear()
    fake.failure = CaldavError(CaldavKind.BUSY, 503)
    r = await client.post("/EWS/Exchange.asmx", content=h.envelope(h.get_folder("inbox")))
    assert r.status_code == 503 and r.headers["retry-after"]


# ------------------------------------------------------------------ cookie leak (the server sets a session cookie after Basic auth)


async def test_no_cookie_leak_between_requests():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        return httpx.Response(207, headers={"Set-Cookie": "nc_session=secret123; Path=/"}, content=b"<d:multistatus xmlns:d='DAV:'/>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        cal = HttpCaldav("https://nc.example.test/remote.php/dav", http)
        await cal.check_access(Credentials("test-user", "pw-eins-1234"))
        await cal.check_access(Credentials("test-user", "pw-zwei-5678"))
        await cal.check_access(Credentials("andere", "pw-drei-9012"))
    assert seen == [None, None, None]


async def test_line_break_at_the_password_end_is_tolerated(services):
    assert (await services.login.check(basic(USER, PASSWORD + "\n"), "10.0.0.1")).account.name == USER
    assert (await services.login.check(basic(USER, PASSWORD + "\r\n"), "10.0.0.1")).account.name == USER
