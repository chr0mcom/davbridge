from __future__ import annotations

import copy
import dataclasses

import pytest

from davbridge.config import Config, from_dict, set_runtime_default_zone


class FakeClock:
    """Controllable clock: sleep() jumps the time forward without waiting."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds

    def before(self, seconds: float) -> None:
        self.t += seconds


TEST_CONFIG = {
    "config_version": 1,
    "server": {"public_url": "https://dav.example.com", "default_timezone": "Europe/Berlin"},
    "caldav": {"base_url": "https://cloud.example.com/remote.php/dav"},
    "auth": {"allowed_users": ["test-user"]},
    "protocols": {"ews": {"stream_max_seconds": 540}},
    "features": {"read_only_owners": ["read-only-owner"], "protected_property_prefixes": ["X-EXAMPLE-PROTECTED-"]},
    "users": [
        {
            "username": "test-user",
            "address": "test-user@dav.example.com",
            "display_name": "Test User",
            "calendar_mode": "explicit",
            "calendars": [
                {"path": "test-calendar", "id": "test-calendar", "display_name": "Test Calendar", "default": True, "write": "caldav", "owner": "test-user"},
                {
                    "path": "read-only-calendar_shared_by_read-only-owner",
                    "id": "read-only-calendar",
                    "display_name": "Read-Only Calendar",
                    "default": False,
                    "write": "never",
                    "owner": "read-only-owner",
                },
            ],
        }
    ],
}


def mix(base: dict, extra: dict) -> dict:
    """Deep merge of nested configuration tables (lists and scalars are replaced)."""
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = mix(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def as_test_accounts(config: Config) -> Config:
    """Marks all accounts as test accounts with capture enabled (the capture path is not reachable from the configuration)."""
    return dataclasses.replace(config, accounts=tuple(dataclasses.replace(k, test=True, capture=True) for k in config.accounts))


@pytest.fixture(autouse=True)
def default_zone_for_tests():
    """The tests express times with daylight-saving transitions; `Europe/Berlin` serves as a neutral DST test case here.
    The runtime default is `UTC` (own tests in test_default_timezone.py)."""
    set_runtime_default_zone("Europe/Berlin")
    yield
    set_runtime_default_zone("UTC")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def config() -> Config:
    return as_test_accounts(from_dict(copy.deepcopy(TEST_CONFIG)))
