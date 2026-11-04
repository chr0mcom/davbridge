"""`protected_property_prefixes` / `protected_event_writable_fields` and `autodiscover_culture` in the EAS path."""

import copy

from tests.conftest import TEST_CONFIG
from tests.doubles.fake_nextcloud import ics
from tests.unit.eas.conftest import USER, collection, k, status, sync_request
from tests.unit.eas.test_eas_http import appointment_data, elements, first_sync, key

from davbridge.config import from_dict
from davbridge.eas import mobile_autodiscover, tags

URI = "test-calendar"
PROTECTED = "X-EXAMPLE-PROTECTED-CREATED-BY:external-writer"
ONLY_SIDE_FIELD = {"features": {"protected_property_prefixes": []}}
WITH_PREFIX = {"features": {"protected_property_prefixes": ["X-EXAMPLE-PROTECTED-"]}}


async def change_with_reminder(c, file="h.ics"):
    cal = c.fake.cal(USER, URI)
    cal.write(file, ics("uid-h", "Title", extra=PROTECTED))
    a = await first_sync(c)
    sid = tags.text_from(elements(a)[0], "AirSync:ServerId")
    a = await c.command(
        "Sync", sync_request(key(a), URI, commands=[k("AirSync:Change", k("AirSync:ServerId", text=sid), appointment_data("New title", start="20261201T090000Z", end="20261201T100000Z", reminder=30))])
    )
    return cal, a


async def test_without_prefix_no_special_effect(eas_bridge_with):
    async with eas_bridge_with(ONLY_SIDE_FIELD) as c:
        cal, a = await change_with_reminder(c)
        assert c.d.config.protected_property_prefixes == ()
        assert "New title" in cal.dav_objects["h.ics"][1] and "VALARM" in cal.dav_objects["h.ics"][1]
        assert PROTECTED in cal.dav_objects["h.ics"][1]


async def test_configured_prefix_protects_as_before(eas_bridge_with):
    async with eas_bridge_with(WITH_PREFIX) as c:
        cal, a = await change_with_reminder(c)
        assert status(tags.find(tags.find(collection(a), "AirSync:Responses"), "AirSync:Change"), "AirSync:Status") == 7
        assert "New title" not in cal.dav_objects["h.ics"][1] and "VALARM" not in cal.dav_objects["h.ics"][1]
        c.d.storage.wait_empty()
        text = "".join(p.read_text() for p in c.d.config.logs_dir.glob("davbridge-*.jsonl"))
        assert '"reason":"protected-property"' in text


async def test_allowed_fields_are_configurable(eas_bridge_with):
    raw = {"features": {"protected_property_prefixes": ["X-EXAMPLE-PROTECTED-"], "protected_event_writable_fields": ["subject", "reminder"]}}
    async with eas_bridge_with(raw) as c:
        cal, a = await change_with_reminder(c)
        assert "New title" in cal.dav_objects["h.ics"][1] and "VALARM" in cal.dav_objects["h.ics"][1]


def pox(address):
    return (
        b"<Autodiscover xmlns='http://schemas.microsoft.com/exchange/autodiscover/mobilesync/requestschema/2006'><Request>"
        b"<EMailAddress>" + address.encode() + b"</EMailAddress></Request></Autodiscover>"
    )


def culture(config):
    response = mobile_autodiscover.pox_response(config, pox("a@dav.example.com"))
    return response.split(b"<Culture>")[1].split(b"</Culture>")[0].decode()


def test_autodiscover_culture_default_and_configured():
    raw = copy.deepcopy(TEST_CONFIG)
    raw["server"].pop("autodiscover_culture", None)
    assert culture(from_dict(raw)) == "en:us"
    raw["server"]["autodiscover_culture"] = "de:de"
    assert culture(from_dict(raw)) == "de:de"
    raw["server"]["autodiscover_culture"] = "fr:fr"
    assert culture(from_dict(raw)) == "fr:fr"
