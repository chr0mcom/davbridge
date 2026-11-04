from pathlib import Path

from davbridge.eas import tags
from davbridge.eas.tags_table import CODEPAGES

FILE = Path(__file__).resolve().parents[2] / "data" / "eas_tags.txt"


def test_tables_match_the_spec_copy():
    expected = {}
    for line in FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        page, page_name, name, token = line.split()
        expected.setdefault(int(page), (page_name, {}))[1].setdefault(int(token, 16), name)
    assert {s: (n, dict(t)) for s, (n, t) in CODEPAGES.items() if t} == expected


def test_core_values():
    assert tags.tag("AirSync:Sync") == (0, 0x05)
    assert tags.tag("FolderHierarchy:FolderSync") == (7, 0x16)
    assert tags.tag("Calendar:Timezone") == (4, 0x05)
    assert tags.tag("Ping:HeartbeatInterval") == (13, 0x08)
    assert tags.tag("Provision:EASProvisionDoc") == (14, 0x0D)
    assert tags.tag("AirSyncBase:Body") == (17, 0x0A)
    assert tags.tag("Settings:UserInformation") == (18, 0x1D)
    assert tags.tag("ComposeMail:SendMail") == (21, 0x05)
    assert tags.name(4, 0x26) == "Calendar:Subject"
    assert tags.name(4, 0x10) is None
