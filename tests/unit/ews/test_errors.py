from pathlib import Path

import pytest

from davbridge import errors
from davbridge.errors import Code, EwsError, from_caldav

FILE = Path(__file__).resolve().parents[2] / "data" / "response_codes.txt"
PINNED = {z.strip() for z in FILE.read_text(encoding="utf-8").splitlines() if z.strip() and not z.startswith("#")}


class _Kind:
    def __init__(self, value):
        self.value = value


class _Error(Exception):
    def __init__(self, kind):
        super().__init__()
        self.kind = _Kind(kind)
        self.status = None


def test_all_codes_are_in_the_pinned_list():
    assert {c.value for c in Code} <= PINNED


def test_code_names_equal_the_values():
    for c in Code:
        assert c.name == c.value


def test_every_code_has_fixed_text_without_placeholder():
    assert set(errors.MESSAGE_TEXT) == set(Code)
    for code, text in errors.MESSAGE_TEXT.items():
        assert "{" not in text and "%" not in text
        assert (text == "") == (code is Code.NoError)


def test_caldav_table_covers_all_kinds():
    expected = {"auth", "forbidden", "not_found", "conflict", "token_invalid", "uid_conflict", "quota", "busy", "network", "unexpected"}
    assert errors.KINDS == expected
    try:
        from davbridge.caldav import CaldavKind
    except ImportError:
        pytest.skip("caldav.py noch not vorhanden")
    assert {a.value for a in CaldavKind} == expected


@pytest.mark.parametrize(
    "kind,code,backoff",
    [
        ("forbidden", Code.ErrorAccessDenied, None),
        ("not_found", Code.ErrorItemNotFound, None),
        ("conflict", Code.ErrorIrresolvableConflict, None),
        ("token_invalid", Code.ErrorInvalidSyncStateData, None),
        ("uid_conflict", Code.ErrorItemSave, None),
        ("quota", Code.ErrorQuotaExceeded, None),
        ("busy", Code.ErrorServerBusy, 30000),
        ("network", Code.ErrorServerBusy, 30000),
        ("unexpected", Code.ErrorInternalServerError, None),
    ],
)
def test_write_mapping(kind, code, backoff):
    f = from_caldav(_Error(kind), writing=True)
    assert isinstance(f, EwsError)
    assert (f.code, f.backoff_ms) == (code, backoff)


def test_reading_never_turns_a_conflict_into_a_conflict():
    assert from_caldav(_Error("conflict"), writing=False).code is Code.ErrorInternalServerError
    assert from_caldav(_Error("uid_conflict"), writing=False).code is Code.ErrorInternalServerError


def test_unknown_kind_is_internal_error():
    assert from_caldav(_Error("unknown-kind"), writing=True).code is Code.ErrorInternalServerError
    assert from_caldav(Exception("x"), writing=True).code is Code.ErrorInternalServerError


def test_ews_error_carries_no_user_data():
    f = EwsError(Code.ErrorServerBusy, backoff_ms=1000)
    assert str(f) == "ErrorServerBusy" and f.args == ("ErrorServerBusy",)
