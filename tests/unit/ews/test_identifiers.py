import pytest

from davbridge.identifiers import ElementRef, IdService, InvalidId

K = bytes(range(32))


@pytest.fixture
def ids():
    return IdService(K)


def test_key_length():
    with pytest.raises(ValueError):
        IdService(b"kurz")


def test_element_id_stable_and_roundtrip(ids):
    ref = ElementRef("test-calendar", "abc.ics")
    a = ids.element_id("u", ref)
    assert a == ids.element_id("u", ref)
    assert ids.read_element_id("u", a) == ref


def test_element_id_forgery_and_account_switch(ids):
    a = ids.element_id("u", ElementRef("test-calendar", "abc.ics"))
    with pytest.raises(InvalidId):
        ids.read_element_id("other", a)
    broken = a[:-2] + ("A" if a[-2] != "A" else "B") + a[-1]
    with pytest.raises(InvalidId):
        ids.read_element_id("u", broken)
    with pytest.raises(InvalidId):
        IdService(bytes(32)).read_element_id("u", a)


def test_type_confusion(ids):
    o = ids.folder_id("u", "test-calendar")
    with pytest.raises(InvalidId):
        ids.read_element_id("u", o)
    e = ids.element_id("u", ElementRef("test-calendar", "a.ics"))
    with pytest.raises(InvalidId):
        ids.read_folder_id("u", e)


def test_change_key_applies_only_to_its_own_item(ids):
    a, b = ElementRef("test-calendar", "a.ics"), ElementRef("test-calendar", "b.ics")
    ck = ids.change_key("u", a, '"etag-1"')
    assert ids.read_change_key("u", a, ck) == '"etag-1"'
    with pytest.raises(InvalidId):
        ids.read_change_key("u", b, ck)


def test_calendar_swap(ids):
    ck = ids.change_key("u", ElementRef("test-calendar", "a.ics"), '"e"')
    with pytest.raises(InvalidId):
        ids.read_change_key("u", ElementRef("read-only-calendar", "a.ics"), ck)


def test_sync_state_binds_folder(ids):
    s = ids.sync_state("u", "test-calendar", b"\x01" * 16)
    assert ids.read_sync_state("u", "test-calendar", s) == b"\x01" * 16
    with pytest.raises(InvalidId):
        ids.read_sync_state("u", "read-only-calendar", s)


def test_subscription_and_watermark(ids):
    a = ids.subscription_id("u", b"epoche", b"abo1")
    assert ids.read_subscription_id("u", a) == (b"epoche", b"abo1")
    w = ids.watermark("u", b"abo1", 7)
    assert ids.read_watermark("u", b"abo1", w) == 7
    with pytest.raises(InvalidId):
        ids.read_watermark("u", b"abo2", w)


@pytest.mark.parametrize("garbage", ["", "!!!", "AAAA", "a" * 5000, None, "ä"])
def test_garbage_is_rejected(ids, garbage):
    with pytest.raises(InvalidId):
        ids.read_element_id("u", garbage)


def test_field_length(ids):
    with pytest.raises(ValueError):
        ids.element_id("u", ElementRef("test-calendar", "x" * 300))


def test_element_hash_stable_short(ids):
    h = ids.element_hash("u", ElementRef("test-calendar", "a.ics"))
    assert len(h) == 12 and h == ids.element_hash("u", ElementRef("test-calendar", "a.ics"))
    assert h != ids.element_hash("u", ElementRef("test-calendar", "b.ics"))


def test_hmac_context_is_davbridge():
    from davbridge import identifiers as module

    assert module.CONTEXT == b"davbridge\x00"
