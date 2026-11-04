import base64

import pytest

from davbridge.eas import timezone as zz

# Field layout per [MS-ASDTYPE] 2.6.4, written down independently of the pack code (W. Europe Standard Time, without names).
BERLIN_RAW = (
    bytes.fromhex("c4ffffff") + bytes(64) + bytes.fromhex("00000a00000005000300000000000000") + bytes(4) + bytes(64) + bytes.fromhex("00000300000005000200000000000000") + bytes.fromhex("c4ffffff")
)
BERLIN = base64.b64encode(BERLIN_RAW).decode()


def test_berlin_from_iana_matches_the_usual_block():
    z = zz.from_iana("Europe/Berlin", 2026)
    assert z.bias == -60 and z.summer_bias == -60 and z.default_bias == 0
    assert z.default == zz.Rule(10, 0, 5, 3, 0)
    assert z.summer == zz.Rule(3, 0, 5, 2, 0)
    assert zz.encode(z) == BERLIN


def test_block_is_mapped_to_berlin():
    z = zz.decode(BERLIN)
    assert zz.iana_for(z, 2026) == "Europe/Berlin"


@pytest.mark.parametrize("iana", ["America/New_York", "UTC", "Asia/Tokyo", "Australia/Sydney", "Asia/Kolkata"])
def test_roundtrip_of_other_zones(iana):
    z = zz.from_iana(iana, 2026)
    back = zz.decode(zz.encode(z))
    assert back.key() == z.key()
    found = zz.iana_for(back, 2026)
    assert found is not None
    assert zz.from_iana(found, 2026).key() == z.key()


def test_new_york_rules():
    z = zz.from_iana("America/New_York", 2026)
    assert z.bias == 300
    assert z.summer == zz.Rule(3, 0, 2, 2, 0)
    assert z.default == zz.Rule(11, 0, 1, 2, 0)


@pytest.mark.parametrize("text", ["", "not base64!", "AAAA", BERLIN[:-8] + "AAAAAAA="])
def test_invalid_blocks(text):
    with pytest.raises(ValueError):
        zz.decode(text)


def test_unknown_block_falls_back_to_fixed_offset():
    z = zz.Timezone(-17, zz.NONE, 0, zz.NONE, 0)
    assert zz.iana_for(z, 2026) is None
    assert zz.fixed_zone(z).utcoffset(None).total_seconds() == 17 * 60


def test_interface_paragraph_5_6():
    import datetime as dt

    reference = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)
    blob = zz.blob_for_iana("Europe/Berlin", reference)
    assert len(blob) == 172 and blob == BERLIN_RAW
    assert zz.iana_for_blob(blob, reference) == "Europe/Berlin"
    assert zz.blob_from_base64(zz.blob_base64("Europe/Berlin", reference)) == blob
    utc = zz.blob_for_iana("UTC", reference)
    assert utc[:4] == bytes(4) and zz.iana_for_blob(utc, reference) == "UTC"
    assert zz.iana_for_blob(b"kurz", reference) is None


def test_all_cldr_zones_find_zone_with_equal_rules():
    import datetime as dt

    from davbridge import timezones

    reference = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    win_iana, _ = timezones._tables()
    for iana in sorted(set(win_iana.values())):
        try:
            z = zz.from_iana(iana, 2026)
        except KeyError:
            continue
        found = zz.iana_for_blob(zz.blob_for_iana(iana, reference), reference)
        assert found is not None, iana
        assert zz.from_iana(found, 2026).key() == z.key() or iana in timezones.UTC_NAMES, iana


def test_half_hour_lord_howe_and_kolkata_without_dst():
    k = zz.from_iana("Asia/Kolkata", 2026)
    assert k.bias == -330 and k.summer.month == 0 and k.default.month == 0
    lh = zz.from_iana("Australia/Lord_Howe", 2026)
    assert lh.summer_bias == -30
