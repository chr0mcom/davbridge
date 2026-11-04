"""The real CalDAV client against recorded (synthetic) Sabre responses, without a network."""

import copy

import httpx
import pytest
from tests.conftest import TEST_CONFIG

from davbridge import caldav
from davbridge.caldav import CaldavError, CaldavKind, Credentials, HttpCaldav
from davbridge.config import from_dict

BASE = "https://cloud.example.com/remote.php/dav"
CAL = from_dict(copy.deepcopy(TEST_CONFIG)).accounts[0].calendar[0]
Z = Credentials("test-user", "pw-test-1234")
PATH = "/remote.php/dav/calendars/test-user/test-calendar/"

MS = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">{content}</d:multistatus>'


def response_with(content: str, token: str = "http://sabre.io/ns/sync/12", status: int = 207):
    return httpx.Response(status, content=(MS.format(content=content + f"<d:sync-token>{token}</d:sync-token>")).encode())


def entry(href: str, etag: str | None, status: str = "HTTP/1.1 200 OK") -> str:
    if etag is None:
        return f"<d:response><d:href>{href}</d:href><d:status>HTTP/1.1 404 Not Found</d:status></d:response>"
    return f"<d:response><d:href>{href}</d:href><d:propstat><d:prop><d:getetag>{etag}</d:getetag></d:prop><d:status>{status}</d:status></d:propstat></d:response>"


def client(handler) -> HttpCaldav:
    return HttpCaldav(BASE, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_sync_reads_changes_and_deletions_with_gzip_etag():
    content = entry(PATH + "a.ics", '"abc-gzip"') + entry(PATH + "b.ics", None) + entry("/foreign/path/c.ics", '"x"')
    c = client(lambda r: response_with(content))
    delta = await c.sync(Z, CAL, None)
    assert delta.changed == {"a.ics": '"abc"'} or list(delta.changed) == ["a.ics"]
    assert delta.deleted == frozenset({"b.ics"}) and delta.full and c.foreign_paths == 1


async def test_sync_follows_507_sections():
    responses = iter(
        [
            response_with(entry(PATH + "a.ics", '"1"') + "<d:response><d:href>/x/</d:href><d:status>HTTP/1.1 507 Insufficient Storage</d:status></d:response>", "http://sabre.io/ns/sync/5"),
            response_with(entry(PATH + "b.ics", '"2"'), "http://sabre.io/ns/sync/6"),
        ]
    )
    delta = await client(lambda r: next(responses)).sync(Z, CAL, None)
    assert sorted(delta.changed) == ["a.ics", "b.ics"] and delta.token.endswith("/6")


async def test_sync_token_jump_back_is_invalid():
    c = client(lambda r: response_with("", "http://sabre.io/ns/sync/3"))
    with pytest.raises(CaldavError) as e:
        await c.sync(Z, CAL, "http://sabre.io/ns/sync/9")
    assert e.value.kind == CaldavKind.TOKEN_INVALID


async def test_response_over_the_limit_is_rejected(monkeypatch):
    monkeypatch.setattr(caldav, "MAX_RESPONSE_BYTES", 100)
    c = client(lambda r: httpx.Response(207, content=b"x" * 500))
    with pytest.raises(CaldavError) as e:
        await c.sync(Z, CAL, None)
    assert e.value.kind == CaldavKind.UNEXPECTED


@pytest.mark.parametrize(
    "href, expected, foreign",
    [
        (PATH + "a%2Db.ics", "a-b.ics", 0),
        (PATH + "a%20b.ics", None, 1),
        ("https://cloud.example.com" + PATH + "a.ics", "a.ics", 0),
        ("https://cloud.example.com", None, 1),
        ("/other/path/a.ics", None, 1),
        (PATH + "../a.ics", None, 1),
    ],
)
def test_file_from_href(href, expected, foreign):
    c = client(lambda r: httpx.Response(200))
    assert c._file_from_href(href, PATH) == expected
    assert c.foreign_paths == foreign


@pytest.mark.parametrize(
    "status, body, kind",
    [
        (403, b"<s:exception>no-uid-conflict</s:exception>", CaldavKind.UID_CONFLICT),
        (409, b"no-uid-conflict", CaldavKind.UID_CONFLICT),
        (403, b"valid-sync-token", CaldavKind.TOKEN_INVALID),
        (404, b"", CaldavKind.NOT_FOUND),
        (412, b"", CaldavKind.CONFLICT),
        (507, b"", CaldavKind.QUOTA),
        (503, b"", CaldavKind.BUSY),
        (401, b"", CaldavKind.AUTH),
    ],
)
def test_status_mapping(status, body, kind):
    assert caldav._error(status, body).kind == kind


async def test_timeout_is_busy():
    def handler(r):
        raise httpx.ReadTimeout("zu langsam")

    with pytest.raises(CaldavError) as e:
        await client(handler).sync(Z, CAL, None)
    assert e.value.kind == CaldavKind.BUSY


def big_answer(entries: int) -> httpx.Response:
    content = "".join(entry(PATH + f"event-{i}.ics", f'"{i}"') for i in range(entries))
    return response_with(content)


async def test_large_answers_are_parsed_off_the_event_loop_and_small_ones_in_place(monkeypatch):
    import threading

    threads: list[threading.Thread] = []
    real = caldav.etree.fromstring

    def spy(content, parser):
        threads.append(threading.current_thread())
        return real(content, parser)

    monkeypatch.setattr(caldav.etree, "fromstring", spy)
    small = await client(lambda r: big_answer(3)).sync(Z, CAL, None)
    assert len(small.changed) == 3 and threads == [threading.main_thread()]
    threads.clear()
    large_answer = big_answer(4000)
    assert len(large_answer.content) > caldav.PARSE_OFF_LOOP_BYTES
    large = await client(lambda r: large_answer).sync(Z, CAL, None)
    assert len(large.changed) == 4000 and len(threads) == 1 and threads[0] is not threading.main_thread()


async def test_the_event_loop_keeps_running_while_a_large_answer_is_parsed(monkeypatch):
    import asyncio
    import time

    real = caldav.etree.fromstring

    def slow(content, parser):
        time.sleep(0.4)
        return real(content, parser)

    monkeypatch.setattr(caldav.etree, "fromstring", slow)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    task = asyncio.create_task(ticker())
    large_answer = big_answer(4000)
    await client(lambda r: large_answer).sync(Z, CAL, None)
    task.cancel()
    assert ticks >= 10


async def test_concurrent_parses_never_share_a_parser_object(monkeypatch):
    import asyncio
    import threading
    import time

    real = caldav.etree.fromstring
    seen: list[tuple[int, int]] = []
    release = threading.Event()
    started = threading.Event()

    def spy(content, parser):
        seen.append((id(parser), threading.get_ident()))
        if len(content) > caldav.PARSE_OFF_LOOP_BYTES:
            started.set()
            release.wait(5)
        return real(content, parser)

    monkeypatch.setattr(caldav.etree, "fromstring", spy)
    large_answer = big_answer(4000)
    small_answer = big_answer(3)
    cal = client(lambda r: large_answer)
    large_task = asyncio.create_task(cal._multistatus(large_answer))
    while not started.is_set():
        await asyncio.sleep(0.01)
    begun = time.monotonic()
    small = await cal._multistatus(small_answer)
    assert time.monotonic() - begun < 2 and small is not None
    release.set()
    await large_task
    await cal._multistatus(small_answer)
    parsers = {parser for parser, _ in seen}
    assert len(seen) == 3 and len(parsers) == 3
    assert len({thread for _, thread in seen}) == 2


async def test_the_size_limit_and_malformed_large_answers_still_apply(monkeypatch):
    broken = httpx.Response(207, content=b"<d:multistatus xmlns:d='DAV:'>" + b"x" * (caldav.PARSE_OFF_LOOP_BYTES + 10))
    with pytest.raises(CaldavError) as e:
        await client(lambda r: broken).sync(Z, CAL, None)
    assert e.value.kind == CaldavKind.UNEXPECTED
