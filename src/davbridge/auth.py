"""Authentication. The CalDAV app password lives in RAM only; it is verified against the CalDAV server, never against a password file of our own."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from .caldav import Caldav, CaldavError, CaldavKind, Credentials
from .clock import Clock
from .config import Account, Config, account_for, dynamic_account, renamed_dynamic_account
from .discovery import Discovery, DiscoveryError
from .identifiers import IdService
from .store import Store

NEGATIVE_S = 900
IDLE_S = 1800
MAX_CACHE = 512
THROTTLE_FAILED_ATTEMPTS = 20


class LoginRejected(Exception):
    def __init__(
        self, reason: Literal["invalid-authorization", "unknown-user", "locked", "wrong-password-cached", "wrong-password", "caldav-unavailable", "throttled", "no-default-calendar"], rest_s: int = 0
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rest_s = rest_s


@dataclass(frozen=True, slots=True)
class Session:
    account: Account
    credentials: Credentials
    ip: str


class Login:
    def __init__(self, config: Config, caldav: Caldav, store: Store, ids: IdService, clock: Clock) -> None:
        self._config = config
        self._caldav = caldav
        self._store = store
        self._ids = ids
        self._clock = clock
        self._positive: dict[bytes, tuple[float, str]] = {}
        self._negative: dict[bytes, float] = {}
        self._accesses: dict[str, list[tuple[Credentials, float]]] = {}
        self._throttle_free: dict[str, float] = {}
        self._last_success: dict[str, list[bytes]] = {}
        self._canonical: dict[str, str] = {}
        self._discovery = Discovery(config, caldav, ids, clock)

    async def _resolved(self, account: Account, credentials: Credentials) -> Account:
        """Fills in the calendars of users without an explicit calendar list (autodiscovery, five-minute metadata cache)."""
        try:
            return await self._discovery.resolve(account, credentials)
        except DiscoveryError:
            raise LoginRejected("no-default-calendar") from None
        except CaldavError:
            raise LoginRejected("caldav-unavailable") from None

    @staticmethod
    def _read_basic(authorization: str | None) -> tuple[str, str]:
        if not authorization or not authorization.lower().startswith("basic "):
            raise LoginRejected("invalid-authorization")
        try:
            raw = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise LoginRejected("invalid-authorization") from None
        user, separator, password = raw.partition(":")
        password = password.rstrip("\r\n")
        if not separator or not user or not password or len(raw) > 512:
            raise LoginRejected("invalid-authorization")
        return user, password

    async def _failed_attempt(self, ip: str) -> None:
        await self._store.lock_failed_attempt(ip)

    async def check(self, authorization: str | None, ip: str, *, namespace: str = "", account_throttle: bool = False) -> Session:
        """Defaults match the EWS behaviour. For EAS, `namespace="eas:"` keeps the IP lock apart from the EWS path and `account_throttle` keeps
        unknown passwords away from the CalDAV server once an account has collected many failed attempts."""
        lock_ip = namespace + ip
        rest = await self._store.lock_check(lock_ip)
        if rest > 0:
            raise LoginRejected("locked", rest)
        if not authorization or not authorization.lower().startswith("basic "):
            raise LoginRejected("invalid-authorization")
        try:
            user, password = self._read_basic(authorization)
        except LoginRejected:
            await self._failed_attempt(lock_ip)
            raise
        account = account_for(self._config, user)
        dynamic = account is None
        if account is None:
            account = dynamic_account(self._config, user)
        if account is None:
            await self._failed_attempt(lock_ip)
            raise LoginRejected("unknown-user")
        if dynamic:
            account = self._canonical_account(account)
        key = self._ids.secret_hash(account.name, password)
        now = self._clock.now()
        credentials = Credentials(account.name, password)
        if self._negative.get(key, 0) > now:
            if account_throttle:
                await self._failed_attempt(lock_ip)
            raise LoginRejected("wrong-password-cached")
        pos = self._positive.get(key)
        if pos and pos[0] > now:
            await self._store.lock_success(lock_ip)
            self._remember_access(credentials)
            return Session(await self._resolved(account, credentials), credentials, ip)
        throttle_hash = self._ids.account_throttle_hash(account.name, password) if account_throttle else b""
        if account_throttle and await self._store.lock_check(f"k:{account.name}") > 0 and throttle_hash not in self._last_success.get(account.name, []):
            if now < self._throttle_free.get(account.name, 0.0):
                await self._failed_attempt(lock_ip)
                raise LoginRejected("throttled")
            self._throttle_free[account.name] = now + 60.0  # at most one CalDAV check per minute: a new password gets through, guessing stays throttled
        try:
            ok = await self._caldav.check_access(credentials)
        except CaldavError as e:
            if e.kind in (CaldavKind.BUSY, CaldavKind.NETWORK):
                raise LoginRejected("caldav-unavailable") from None
            raise LoginRejected("caldav-unavailable") from None
        if not ok:
            self._limit(self._negative)
            self._negative[key] = now + NEGATIVE_S
            await self._failed_attempt(lock_ip)
            if account_throttle:
                await self._store.lock_failed_attempt(f"k:{account.name}", failed_attempts=THROTTLE_FAILED_ATTEMPTS)
            raise LoginRejected("wrong-password")
        if dynamic and account.name.lower() not in self._canonical:
            account, credentials = await self._adopt_server_name(account, credentials)
            key = self._ids.secret_hash(account.name, password)
            throttle_hash = self._ids.account_throttle_hash(account.name, password) if account_throttle else b""
        self._limit(self._positive)
        self._positive[key] = (now + self._config.positive_cache_seconds, account.name)
        self._negative.pop(key, None)
        if account_throttle:
            listing = [h for h in self._last_success.get(account.name, []) if h != throttle_hash]
            self._last_success[account.name] = ([throttle_hash] + listing)[:4]
        await self._store.lock_success(lock_ip)
        self._remember_access(credentials)
        return Session(await self._resolved(account, credentials), credentials, ip)

    def _canonical_account(self, account: Account) -> Account:
        """Pattern mode: `Alice`, `alice` and `ALICE@host` are one user. The server's spelling of the user id replaces the typed one."""
        name = self._canonical.get(account.name.lower())
        if name is None or name == account.name:
            return account
        return renamed_dynamic_account(self._config, account, name)

    async def _adopt_server_name(self, account: Account, credentials: Credentials) -> tuple[Account, Credentials]:
        try:
            name = await self._caldav.canonical_user(credentials)
        except CaldavError:
            raise LoginRejected("caldav-unavailable") from None
        self._limit(self._canonical)
        self._canonical[account.name.lower()] = name or account.name
        account = self._canonical_account(account)
        return account, Credentials(account.name, credentials.password)

    def _limit(self, cache: dict) -> None:
        if len(cache) >= MAX_CACHE:
            for k in list(cache)[: MAX_CACHE // 2]:
                cache.pop(k, None)

    def _remember_access(self, credentials: Credentials) -> None:
        """Up to four valid credentials per account at the same time (Mac password, iPhone password, ...); the most recently used comes first."""
        listing = [e for e in self._accesses.get(credentials.user, []) if e[0].password != credentials.password]
        self._accesses[credentials.user] = ([(credentials, self._clock.now())] + listing)[:4]

    def access_for(self, account: str) -> Credentials | None:
        listing = self._accesses.get(account)
        return listing[0][0] if listing else None

    def forget(self, account: str, credentials: Credentials | None = None) -> None:
        """Without `credentials`: forget everything about the account (EWS behaviour). With `credentials`: only that password that became invalid."""
        if credentials is None:
            self._discovery.discard(account)
            self._accesses.pop(account, None)
            for k in [k for k, (_, name) in self._positive.items() if name == account]:
                self._positive.pop(k, None)
            return
        rest = [e for e in self._accesses.get(account, []) if e[0].password != credentials.password]
        if rest:
            self._accesses[account] = rest
        else:
            self._accesses.pop(account, None)
        self._positive.pop(self._ids.secret_hash(account, credentials.password), None)

    def clean_up(self, keep: Collection[str] = ()) -> None:
        now = self._clock.now()
        for account, listing in list(self._accesses.items()):
            if account not in keep and now - max(z for _, z in listing) > IDLE_S:
                self.forget(account)
        for k, until in list(self._negative.items()):
            if until <= now:
                del self._negative[k]
        for k, (until, _) in list(self._positive.items()):
            if until <= now:
                del self._positive[k]
