"""Integration tests against a real CalDAV server (a throw-away Nextcloud with a synthetic user; see .github/workflows/integration.yml).

The target comes from the environment: DAVBRIDGE_IT_CALDAV_URL, DAVBRIDGE_IT_USER, DAVBRIDGE_IT_PASSWORD, DAVBRIDGE_IT_CALENDAR (writable
calendar path) and DAVBRIDGE_IT_READ_ONLY_CALENDAR (calendar path that is read-only for the user). Plain http is accepted only with
DAVBRIDGE_ALLOW_INSECURE_HTTP=true. Without these variables the tests are skipped.
"""

from __future__ import annotations

import os

import httpx
import pytest

from davbridge.caldav import Credentials, HttpCaldav
from davbridge.config import CalendarConfig, from_dict

NAMES = ("DAVBRIDGE_IT_CALDAV_URL", "DAVBRIDGE_IT_USER", "DAVBRIDGE_IT_PASSWORD", "DAVBRIDGE_IT_CALENDAR", "DAVBRIDGE_IT_READ_ONLY_CALENDAR")


def pytest_collection_modifyitems(config, items):
    if all(os.environ.get(n) for n in NAMES):
        return
    skip = pytest.mark.skip(reason="DAVBRIDGE_IT_* environment variables are not set")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def real_config():
    user = os.environ["DAVBRIDGE_IT_USER"]
    return from_dict(
        {
            "config_version": 1,
            "server": {"public_url": "https://dav.example.com"},
            "caldav": {"base_url": os.environ["DAVBRIDGE_IT_CALDAV_URL"]},
            "auth": {"allowed_users": [user]},
            "users": [
                {
                    "username": user,
                    "calendars": [
                        {"path": os.environ["DAVBRIDGE_IT_CALENDAR"], "id": "test-calendar", "display_name": "Test Calendar", "default": True, "write": "caldav"},
                        {"path": os.environ["DAVBRIDGE_IT_READ_ONLY_CALENDAR"], "id": "read-only-calendar", "display_name": "Read-Only Calendar", "write": "caldav"},
                    ],
                }
            ],
        },
        os.environ,
    )


@pytest.fixture
def credentials() -> Credentials:
    return Credentials(os.environ["DAVBRIDGE_IT_USER"], os.environ["DAVBRIDGE_IT_PASSWORD"])


@pytest.fixture
async def caldav(real_config):
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5), limits=httpx.Limits(max_connections=10)) as http:
        yield HttpCaldav(real_config.caldav_base_url, http)


@pytest.fixture
def test_calendar(real_config) -> CalendarConfig:
    return real_config.accounts[0].calendar_after_key("test-calendar")


@pytest.fixture
def read_only(real_config) -> CalendarConfig:
    return real_config.accounts[0].calendar_after_key("read-only-calendar")
