"""Shared helpers for the tests of ical, timezones and permissions. All data is synthetic."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import icalendar
from lxml import etree

T_NS = "http://schemas.microsoft.com/exchange/services/2006/types"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GOLDEN = FIXTURES / "ics"
GOLDEN_EAS = FIXTURES / "eas"
NOW = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
UTC = dt.timezone.utc


def golden(name: str) -> str:
    return (GOLDEN / name).read_bytes().decode("utf-8")


def ews(xml: str) -> etree._Element:
    """An EWS fragment with the prefix t: bound, as an element (first child of a root element)."""
    root = etree.fromstring(f'<w xmlns:t="{T_NS}">{xml}</w>')
    return root[0]


def item_change(*updates: str) -> etree._Element:
    return ews(f"<t:ItemChange><t:ItemId Id='x' ChangeKey='y'/><t:Updates>{''.join(updates)}</t:Updates></t:ItemChange>")


def set_field(uri: str, value_xml: str) -> str:
    return f"<t:SetItemField><t:FieldURI FieldURI='{uri}'/><t:CalendarItem>{value_xml}</t:CalendarItem></t:SetItemField>"


def delete(uri: str) -> str:
    return f"<t:DeleteItemField><t:FieldURI FieldURI='{uri}'/></t:DeleteItemField>"


def vevent(ics: str) -> icalendar.Event:
    return icalendar.Calendar.from_ical(ics).walk("VEVENT")[0]


def properties(component: icalendar.Component, except_: frozenset[str] = frozenset()) -> list[tuple[str, tuple, bytes]]:
    """All properties of a component (without subcomponents) as sorted (name, parameters, ical value)."""
    result = []
    for name, value in component.items():
        if name.upper() in except_:
            continue
        for single in value if isinstance(value, list) else [value]:
            parameter = tuple(sorted((str(k), str(v)) for k, v in getattr(single, "params", {}).items()))
            raw = single.to_ical()
            result.append((name.upper(), parameter, raw if isinstance(raw, bytes) else raw.encode()))
    return sorted(result)


def alarms(ics: str) -> list[list[tuple[str, tuple, bytes]]]:
    return [properties(a) for a in vevent(ics).walk("VALARM")]


def utc(year: int, month: int, tag: int, hour: int = 0, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, tag, hour, minute, tzinfo=UTC)
