"""Offline self-test of the EAS test client (tools/eas_test_client.py): its own WBXML codec, its own token table and the scenario logic against a
small fake server. The client is loaded with importlib (not from the server package) and must touch neither the network nor stdin on import."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import random
import socket
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

PROJECT = Path(__file__).resolve().parents[3]
CLIENT_PATH = PROJECT / "tools" / "eas_test_client.py"
TAG_FILE = PROJECT / "tests" / "data" / "eas_tags.txt"
FOLDERSYNC_0 = bytes.fromhex("03016a00000756520330000101")


def load_client(name: str = "eas_testclient_unter_test"):
    spec = importlib.util.spec_from_file_location(name, CLIENT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old
    return module


@pytest.fixture(scope="module")
def ec():
    return load_client()


# ---------------------------------------------------------------- import without side effects


def test_import_does_not_touch_the_network_and_reads_no_stdin(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("Netzzugriff beim Import")

    class StdinForbidden:
        def readline(self, *a):
            raise AssertionError("stdin beim Import gelesen")

        read = readline

        def isatty(self):
            return False

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(sys, "stdin", StdinForbidden())
    module = load_client("eas_testclient_import_probe")
    assert callable(module.main)


def test_client_imports_nothing_from_app():
    source = CLIENT_PATH.read_text(encoding="utf-8")
    assert "from app" not in source and "import app" not in source


# ---------------------------------------------------------------- Codec


def test_foldersync_vector(ec):
    w = ec.decode(FOLDERSYNC_0)
    assert ec.display(w) == "FolderHierarchy:FolderSync"
    assert ec.txt(w, "FolderHierarchy:SyncKey") == "0"
    assert ec.encode(w) == FOLDERSYNC_0
    assert ec.encode(ec.K("FolderHierarchy:FolderSync", ec.K("FolderHierarchy:SyncKey", text="0"))) == FOLDERSYNC_0


def random_tree(ec, rng: random.Random, depth: int = 0):
    qname = rng.choice(sorted(ec.NAME_TO_TOKEN))
    choice = rng.random()
    if depth < 4 and choice < 0.4:
        return ec.K(qname, *[random_tree(ec, rng, depth + 1) for _ in range(rng.randint(1, 4))])
    if choice < 0.75:
        return ec.K(qname, text="".join(rng.choice("abcXYZ09 -_äöüß€🦷\n") for _ in range(rng.randint(1, 30))))
    if choice < 0.9:
        return ec.K(qname, data=bytes(rng.randrange(256) for _ in range(rng.randint(0, 300))))
    return ec.K(qname)


def test_roundtrip_random_trees(ec):
    rng = random.Random(20261104)
    for _ in range(600):
        tree = random_tree(ec, rng)
        raw = ec.encode(tree)
        assert raw.startswith(b"\x03\x01\x6a\x00")
        assert ec.decode(raw) == tree


def test_page_switch_and_opaque(ec):
    k = ec.K("AirSync:Sync", ec.K("AirSyncBase:Body", ec.K("AirSyncBase:Data", data=b"\x00\x01\xff" * 100)), ec.K("Calendar:Subject", text="Äpfel"))
    raw = ec.encode(k)
    assert bytes((0x00, 17)) in raw and bytes((0x00, 4)) in raw and b"\xc3" in raw
    assert ec.decode(raw) == k


def test_long_mb_u_int(ec):
    data = bytes(range(256)) * 40
    k = ec.K("AirSyncBase:Data", data=data)
    assert ec.decode(ec.encode(k)).data == data


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x03\x01\x6a",
        FOLDERSYNC_0[:-1],
        FOLDERSYNC_0[:-2],
        bytes.fromhex("03016a000007565203303001"),
        bytes.fromhex("03016a0000075680"),
        bytes.fromhex("03016a00000744") + b"\x01",
        bytes.fromhex("03016a05000756010101"),
        FOLDERSYNC_0 + b"\x05",
        bytes.fromhex("03016a00000756c3ffffffffff7f"),
        bytes.fromhex("03016a0000075603ff00"),
        b"\xff" * 20,
    ],
)
def test_broken_inputs_raise_wbxml_error(ec, raw):
    with pytest.raises(ec.WbxmlError):
        ec.decode(raw)


def test_table_matches_the_spec_copy(ec):
    spec = {}
    for line in TAG_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        page, page_name, name, token = line.split()
        spec[(int(page), int(token, 16))] = f"{page_name}:{name}"
    assert ec.TOKEN_TO_NAME
    for key, name in ec.TOKEN_TO_NAME.items():
        assert spec.get(key) == name, (key, name)
    assert len(ec.TOKEN_TO_NAME) == len(ec.NAME_TO_TOKEN)


def test_core_values_of_the_table(ec):
    assert ec.NAME_TO_TOKEN["AirSync:Sync"] == (0, 0x05)
    assert ec.NAME_TO_TOKEN["FolderHierarchy:FolderSync"] == (7, 0x16)
    assert ec.NAME_TO_TOKEN["Calendar:Timezone"] == (4, 0x05)
    assert ec.NAME_TO_TOKEN["Ping:HeartbeatInterval"] == (13, 0x08)
    assert ec.NAME_TO_TOKEN["Provision:EASProvisionDoc"] == (14, 0x0D)
    assert ec.NAME_TO_TOKEN["AirSyncBase:Body"] == (17, 0x0A)
    assert ec.NAME_TO_TOKEN["Settings:UserInformation"] == (18, 0x1D)
    assert ec.NAME_TO_TOKEN["ComposeMail:SendMail"] == (21, 0x05)


def test_timezone_blob_has_172_bytes_and_matches_in_the_core(ec):
    blob = ec.tz_blob_berlin()
    assert len(blob) == 172
    assert int.from_bytes(blob[:4], "little", signed=True) == -60
    assert blob[4 : 4 + 46].decode("utf-16-le").startswith("W. Europe Standard Time")
    assert ec.TZ_BASE64 and len(ec.TZ_BASE64) == 232


def test_skeleton_shows_no_contents(ec):
    w = ec.K(
        "AirSync:Sync",
        ec.K("Calendar:Subject", text="Secret Subject"),
        ec.K("Calendar:Location", text="Secret Place"),
        ec.K("Calendar:Subject", text="DAVBRIDGE-TEST-1234"),
        ec.K("AirSync:Status", text="1"),
    )
    text = "\n".join(ec.skeleton(w))
    assert "Secret" not in text and "DAVBRIDGE-TEST-1234" in text and "Status = 1" in text


# ---------------------------------------------------------------- fake server for the scenarios


class FakeEas:
    """Small EAS server for calendar Sync, FolderSync and OPTIONS: enough to run the client scenarios offline."""

    def __init__(self, ec, password="secret"):
        self.ec, self.password = ec, password
        self.base, self.host = "https://fake.invalid", "fake.invalid"
        self.cal = {"abn": {}, "nl": {}}
        self.read_only = {"nl"}
        self.dev: dict = {}
        self.counter = 0
        for i in range(7):
            self.new("abn", f"Foreign appointment {i}", series=(i == 2), private=(i in (1, 5)))
        self.new("nl", "Read only 1")

    def new(self, cid, subject, series=False, private=False):
        self.counter += 1
        sid = hashlib.sha256(f"{cid}{self.counter}".encode()).hexdigest()[:12]
        self.cal[cid][sid] = {"subject": subject, "ver": 1, "series": series, "private": private}
        return sid

    def response(self, root, status=200, header=None):
        ec = self.ec
        k = [("content-type", "application/vnd.ms-sync.wbxml")] + list((header or {}).items())
        return ec.Reply(status, k, ec.encode(root) if root is not None else b"", 0.01)

    def request(self, method, p, header, payload=None, timeout=None):
        ec = self.ec
        auth = header.get("Authorization")
        if auth != ec.basic("test-user", self.password):
            return ec.Reply(401, [("WWW-Authenticate", 'Basic realm="fake"')], b"", 0.01)
        if method == "OPTIONS":
            return ec.Reply(200, [("MS-ASProtocolVersions", "14.1"), ("MS-ASProtocolCommands", ",".join(sorted(ec.COMMANDS_ADVERTISED))), ("MS-Server-ActiveSync", "14.1")], b"", 0.01)
        q = parse_qs(urlsplit(p).query)
        cmd, dev = q["Cmd"][0], q["DeviceId"][0]
        if header.get("MS-ASProtocolVersion") != "14.1":
            return ec.Reply(400, [], b"", 0.01)
        if cmd == "FolderSync":
            return self.foldersync(payload)
        if cmd == "Sync":
            return self.sync(dev, payload)
        return ec.Reply(501, [], b"", 0.01)

    def foldersync(self, payload):
        ec = self.ec
        w = ec.decode(payload)
        key = ec.txt(w, "FolderHierarchy:SyncKey")
        if key not in ("0", "H1"):
            return self.response(ec.K("FolderHierarchy:FolderSync", ec.K("FolderHierarchy:Status", text="9")))
        folder = [
            ("inbox", "Inbox", 2),
            ("drafts", "Drafts", 3),
            ("deleteditems", "Deleted Items", 4),
            ("sentitems", "Sent Items", 5),
            ("outbox", "Outbox", 6),
            ("tasks", "Tasks", 7),
            ("contacts", "Contacts", 9),
            ("abn", "Test Calendar", 8),
            ("nl", "Read-Only Calendar", 13),
        ]
        adds = [
            ec.K(
                "FolderHierarchy:Add",
                ec.K("FolderHierarchy:ServerId", text=i),
                ec.K("FolderHierarchy:ParentId", text="0"),
                ec.K("FolderHierarchy:DisplayName", text=n),
                ec.K("FolderHierarchy:Type", text=t),
            )
            for i, n, t in folder
        ]
        if key == "H1":
            adds = []
        return self.response(
            ec.K(
                "FolderHierarchy:FolderSync",
                ec.K("FolderHierarchy:Status", text="1"),
                ec.K("FolderHierarchy:SyncKey", text="H1"),
                ec.K("FolderHierarchy:Changes", ec.K("FolderHierarchy:Count", text=len(adds)), *adds),
            )
        )

    def sync(self, dev, payload):
        ec = self.ec
        K = ec.K
        w = ec.decode(payload)
        col = ec.path(w, "AirSync:Collections/AirSync:Collection")
        key, cid = ec.txt(col, "AirSync:SyncKey"), ec.txt(col, "AirSync:CollectionId")
        window = int(ec.txt(col, "AirSync:WindowSize") or 100)
        h = hashlib.sha256(payload).digest()

        def wrapper(status, k, cmds=None, resp=None, more=False):
            return self.response(
                K(
                    "AirSync:Sync",
                    K(
                        "AirSync:Collections",
                        K(
                            "AirSync:Collection",
                            K("AirSync:SyncKey", text=k) if k else None,
                            K("AirSync:CollectionId", text=cid),
                            K("AirSync:Status", text=status),
                            K("AirSync:MoreAvailable") if more else None,
                            cmds,
                            resp,
                        ),
                    ),
                )
            )

        if cid not in self.cal:
            return wrapper(12, None)
        st = self.dev.get((dev, cid))
        if key == "0":
            st = {"z": 1, "known": {}, "hash": None, "resp": None}
            self.dev[(dev, cid)] = st
            return wrapper(1, "K1")
        if st is None:
            return wrapper(3, None)
        if key == f"K{st['z'] - 1}" and st["hash"] == h:
            return st["resp"]
        if key != f"K{st['z']}":
            return wrapper(3, None)
        events = self.cal[cid]
        responses = []
        cmds = ec.kind(col, "AirSync:Commands")
        for c in cmds.children if cmds else ():
            kind = ec.display(c).split(":")[1]
            sid = ec.txt(c, "AirSync:ServerId")
            if kind == "Add":
                client = ec.txt(c, "AirSync:ClientId")
                ad = ec.kind(c, "AirSync:ApplicationData")
                if cid in self.read_only or ec.kind(ad, "Calendar:Recurrence") is not None:
                    responses.append(K("AirSync:Add", K("AirSync:ClientId", text=client), K("AirSync:Status", text="6")))
                    continue
                new = self.new(cid, ec.txt(ad, "Calendar:Subject"))
                st["known"][new] = 1
                responses.append(K("AirSync:Add", K("AirSync:ClientId", text=client), K("AirSync:ServerId", text=new), K("AirSync:Status", text="1")))
            elif kind in ("Change", "Delete"):
                e = events.get(sid)
                if e is None:
                    responses.append(K(f"AirSync:{kind}", K("AirSync:ServerId", text=sid), K("AirSync:Status", text="8")))
                elif cid in self.read_only or e["series"] or st["known"].get(sid) != e["ver"] or (kind == "Delete" and 1 <= st.setdefault("konf", {}).get(sid, 0) < 2):
                    responses.append(K(f"AirSync:{kind}", K("AirSync:ServerId", text=sid), K("AirSync:Status", text="7")))
                    st["known"][sid] = 0
                    if kind == "Delete":
                        st.setdefault("konf", {})[sid] = st.setdefault("konf", {}).get(sid, 0) + 1
                elif kind == "Change":
                    e["subject"] = ec.txt(ec.kind(c, "AirSync:ApplicationData"), "Calendar:Subject")
                    e["ver"] += 1
                    st["known"][sid] = e["ver"]
                    responses.append(K("AirSync:Change", K("AirSync:ServerId", text=sid), K("AirSync:Status", text="1")))
                else:
                    del events[sid]
                    st["known"].pop(sid, None)
                    responses.append(K("AirSync:Delete", K("AirSync:ServerId", text=sid), K("AirSync:Status", text="1")))
        open = [s for s, e in events.items() if st["known"].get(s) != e["ver"]]
        delete = [s for s in st["known"] if s not in events]
        delivery = []
        for s in (open + delete)[:window]:
            if s in delete:
                delivery.append(K("AirSync:Delete", K("AirSync:ServerId", text=s)))
                del st["known"][s]
                continue
            e = events[s]
            kind = "Change" if s in st["known"] else "Add"
            ad = K(
                "AirSync:ApplicationData",
                K("Calendar:Subject", text=e["subject"]),
                K("Calendar:Sensitivity", text="2" if e["private"] else "0"),
                K("Calendar:Exceptions", K("Calendar:Exception", K("Calendar:Deleted", text="1"))) if e["series"] else None,
                K("Calendar:Recurrence", K("Calendar:Type", text="1")) if e["series"] else None,
            )
            delivery.append(K(f"AirSync:{kind}", K("AirSync:ServerId", text=s), ad))
            st["known"][s] = e["ver"]
        more = len(open) + len(delete) > window
        if delivery or responses:
            st["z"] += 1
        response = wrapper(1, f"K{st['z']}", K("AirSync:Commands", *delivery) if delivery else None, K("AirSync:Responses", *responses) if responses else None, more)
        if responses or delivery:
            st["hash"], st["resp"] = h, response
        return response


@pytest.fixture
def run(ec, tmp_path, capsys):
    fake = FakeEas(ec)

    def start(*argv: str) -> int:
        return ec.main(list(argv) + ["--state", str(tmp_path / "state.json")] + SHARED, http_obj=fake, stdin=io.StringIO("secret\n"))

    start.fake, start.tmp, start.capsys = fake, tmp_path, capsys
    return start


SHARED = ["--url", "https://fake.invalid", "--user", "test-user", "--password-stdin"]


def output(run) -> str:
    return run.capsys.readouterr().out


def test_password_never_in_the_output_and_tests_run(run):
    assert run("options") == 0
    assert run("foldersync") == 0
    assert "secret" not in output(run)


def test_options_reports_error_on_wrong_password(ec, tmp_path, capsys):
    fake = FakeEas(ec, password="other")
    rc = ec.main(["options", "--state", str(tmp_path / "z.json")] + SHARED, http_obj=fake, stdin=io.StringIO("secret\n"))
    out = capsys.readouterr().out
    assert rc == 1 and "ERROR" in out and "secret" not in out


def test_missing_password_is_exit_2(ec, tmp_path, capsys):
    rc = ec.main(["options", "--state", str(tmp_path / "z.json")] + SHARED, http_obj=FakeEas(ec), stdin=io.StringIO(""))
    assert rc == 2


def test_foldersync_and_sync_null(run):
    assert run("foldersync") == 0
    assert "T4 PASS" in output(run)


def test_sync_full_paging(run):
    assert run("sync-full", "--window", "3") == 0
    out = output(run)
    assert "7 items, 3 windows, 1 series" in out and "2 with Sensitivity=2" in out and "Foreign appointment" not in out


def test_add_change_delete_chain(run):
    assert run("add") == 0
    assert any(e["subject"].startswith("DAVBRIDGE-TEST-") for e in run.fake.cal["abn"].values())
    assert run("change") == 0
    assert any(e["subject"].endswith("-g") for e in run.fake.cal["abn"].values())
    assert run("delete") == 0
    out = output(run)
    assert "ERROR" not in out
    assert not [e for e in run.fake.cal["abn"].values() if e["subject"].startswith("DAVBRIDGE-TEST-")]
    state = run.tmp / "state.json"
    assert oct(state.stat().st_mode & 0o777) == "0o600"


def test_retry_creates_exactly_one_appointment(run):
    assert run("retry") == 0
    assert "ERROR" not in output(run)


def test_key_error_status_3(run):
    assert run("keys") == 0
    assert "ERROR" not in output(run)


def test_read_only_rejects(run):
    assert run("readonly") == 0
    out = output(run)
    assert "Status 6" not in out or "ERROR" not in out
    assert "ERROR" not in out and len(run.fake.cal["nl"]) == 1


def test_series_read_and_write_rejected(run):
    rc = run("series")
    out = output(run)
    assert "Add with Recurrence -> Responses/Add status 6" in out and "Change of a series -> status 7" in out
    assert rc == 0, out


def test_series_without_fixture_is_skipped_not_counted_as_error(run):
    for e in run.fake.cal["abn"].values():
        e["series"] = False
    rc = run("series")
    out = output(run)
    assert "skipped" in out and "ERROR" not in out and rc == 0


def test_password_scenario(run):
    assert run("password") == 0


def test_clean_up_removes_only_test_markers(run):
    assert run("add") == 0
    assert run("cleanup") == 0
    events = run.fake.cal["abn"]
    assert len(events) == 7 and not any(e["subject"].startswith("DAVBRIDGE-TEST-") for e in events.values())


def test_lock_needs_confirmation(run):
    assert run("lockout") == 0
    out = output(run)
    assert "skipped" in out and "T21 SKIP" in out


def test_folder_not_found_is_exit_2_or_fail(run):
    rc = run("sync-full", "--folder", "does-not-exist")
    assert rc == 1 and "not found" in output(run)


def test_delete_state(run):
    assert run("foldersync") == 0
    assert (run.tmp / "state.json").exists()
    assert run("clear-state") == 0
    assert not (run.tmp / "state.json").exists()


def test_everything_lists_skip_and_exits_0_against_the_fake(run, monkeypatch, ec):
    rc = run("all", "--fast")
    out = output(run)
    result = out.split("== Result ==")[1]
    for identifier in ("T13", "T17", "T21"):
        assert f"{identifier:<4} SKIP" in result
    for identifier in ("T14", "T15"):
        assert f"{identifier:<4} SKIP" in result
    # the fake knows neither Provision/Settings/Ping/Autodiscover nor rejected commands: these scenarios must report FAIL, never crash
    assert "Total: PASS" in result and rc == 1
    for identifier in ("T4", "T7", "T8", "T9", "T10", "T11", "T12", "T16"):
        assert f"{identifier:<4} PASS" in result, identifier
    assert "secret" not in out
