"""Permissions: never blanket. Folder = configuration x policy x CalDAV server; an item additionally by content."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from ics_helpers import T_NS, golden

from davbridge import ical as m
from davbridge import permissions as r
from davbridge.config import CalendarConfig

POLICY = frozenset({"read-only-owner"})


def cal(owner="test-user", write="caldav"):
    return CalendarConfig(uri="k", key="k", display_name="K", default=False, write=write, owner=owner)


def info(writable=True):
    return SimpleNamespace(writable=writable)


def appointment(name):
    return m.read(golden(name))[0]


def test_own_writable_calendar():
    permissions = r.folder_permissions(cal(), info(), POLICY)
    assert permissions == r.Permissions(read=True, change=True, delete=True, create=True, is_organizer=False)


@pytest.mark.parametrize(
    "calendar, nc_writable",
    [
        (cal(owner="read-only-owner"), True),  # the policy beats the CalDAV server
        (cal(write="never"), True),  # the configuration beats the CalDAV server
        (cal(), False),  # the CalDAV server denies
        (cal(owner="read-only-owner", write="never"), False),
    ],
)
def test_folder_is_read_only_if_any_rule_forbids_it(calendar, nc_writable):
    permissions = r.folder_permissions(calendar, info(nc_writable), POLICY)
    assert permissions.read is True
    assert not (permissions.change or permissions.delete or permissions.create or permissions.is_organizer)


def test_imported_calendar_stays_read_only_even_if_nextcloud_allows_writing():
    imported = cal(owner="read-only-owner", write="caldav")
    assert r.folder_permissions(imported, info(True), POLICY).change is False


def test_item_in_writable_folder_is_changeable_and_organizer():
    folder = r.folder_permissions(cal(), info(), POLICY)
    permissions = r.element_permissions(folder, appointment("custom-properties-single.ics"))
    assert permissions == r.Permissions(True, True, True, True, True)


@pytest.mark.parametrize("name", ["with-attendees.ics"])
def test_appointment_with_attendees_is_readable_but_not_changeable(name):
    folder = r.folder_permissions(cal(), info(), POLICY)
    permissions = r.element_permissions(folder, appointment(name))
    assert permissions.read is True
    assert not (permissions.change or permissions.delete or permissions.is_organizer)


@pytest.mark.parametrize("name", ["imported-series.ics", "imported-exception.ics", "vtodo.ics", "broken.ics"])
def test_unrepresentable_has_no_permissions_at_all(name):
    folder = r.folder_permissions(cal(), info(), POLICY)
    assert r.element_permissions(folder, appointment(name)) == r.NONE


def test_item_in_read_only_folder_is_never_organizer():
    folder = r.folder_permissions(cal(owner="read-only-owner"), info(), POLICY)
    permissions = r.element_permissions(folder, appointment("imported-single.ics"))
    assert permissions.read and not (permissions.change or permissions.delete or permissions.is_organizer)


def test_is_organizer_always_follows_the_change_permission():
    for folder_cal in (cal(), cal(owner="read-only-owner")):
        folder = r.folder_permissions(folder_cal, info(), POLICY)
        for name in ("custom-properties-single.ics", "with-attendees.ics", "imported-single.ics"):
            permissions = r.element_permissions(folder, appointment(name))
            assert permissions.is_organizer == permissions.change


def test_effective_rights_folder_and_item():
    writable = r.Permissions(True, True, True, True, True)
    folder = r.effective_rights(writable, folder=True)
    values = {e.tag.split("}")[1]: e.text for e in folder}
    assert folder.tag == f"{{{T_NS}}}EffectiveRights"
    assert values == {"CreateAssociated": "false", "CreateContents": "true", "CreateHierarchy": "false", "Delete": "false", "Modify": "false", "Read": "true"}
    element = {e.tag.split("}")[1]: e.text for e in r.effective_rights(r.Permissions(True, False, False, False, False), folder=False)}
    assert element["Modify"] == "false" and element["Delete"] == "false" and element["Read"] == "true"
    element = {e.tag.split("}")[1]: e.text for e in r.effective_rights(writable, folder=False)}
    assert element["Modify"] == "true" and element["Delete"] == "true"
