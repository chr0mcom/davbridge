"""Permission calculation: EffectiveRights/IsOrganizer from policy x CalDAV permissions x item. Pure, never blanket."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Collection

from lxml import etree

from .config import CalendarConfig

if TYPE_CHECKING:
    from .caldav import CalendarInfo
    from .ical import Appointment

TYPES_NS = "http://schemas.microsoft.com/exchange/services/2006/types"


@dataclass(frozen=True, slots=True)
class Permissions:
    read: bool
    change: bool
    delete: bool
    create: bool
    is_organizer: bool


NONE = Permissions(False, False, False, False, False)


def folder_permissions(cal: CalendarConfig, info: CalendarInfo, policy: Collection[str]) -> Permissions:
    """Writing only if the configuration, the policy (owner) and the CalDAV server all allow it. Reading applies to every configured calendar."""
    write = cal.write == "caldav" and cal.owner not in policy and bool(info.writable)
    return Permissions(read=True, change=write, delete=write, create=write, is_organizer=False)


def element_permissions(folder: Permissions, appointment: Appointment) -> Permissions:
    """Change/delete only for representable single appointments without organizer and attendees in a writable folder.

    `is_organizer` only applies to exactly these own appointments; otherwise the appointment stays readable but not changeable."""
    if not folder.read or not appointment.representable:
        return NONE
    changeable = folder.change and not appointment.has_organizer and not appointment.has_attendee
    return Permissions(
        read=True,
        change=changeable,
        delete=changeable and folder.delete,
        create=folder.create,
        is_organizer=changeable,
    )


def _true(value: bool) -> str:
    return "true" if value else "false"


def effective_rights(permissions: Permissions, *, folder: bool) -> etree._Element:
    """`t:EffectiveRights`. Folder: only reading and creating content, no hierarchy, rename or delete rights on the calendar itself."""
    root = etree.Element(f"{{{TYPES_NS}}}EffectiveRights", nsmap={"t": TYPES_NS})
    values = (
        ("CreateAssociated", False),
        ("CreateContents", permissions.create if folder else False),
        ("CreateHierarchy", False),
        ("Delete", False if folder else permissions.delete),
        ("Modify", False if folder else permissions.change),
        ("Read", permissions.read),
    )
    for name, value in values:
        etree.SubElement(root, f"{{{TYPES_NS}}}{name}").text = _true(value)
    return root
