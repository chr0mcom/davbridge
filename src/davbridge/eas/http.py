"""ASGI handler of the EAS path: method, login, query, body, dispatch, error frame, log and capture file."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from ..auth import LoginRejected
from ..caldav import CaldavError, CaldavKind
from . import skeleton as skeleton_module
from . import tags, wbxml
from .commands import ADVERTISED, COMMANDS, EasContext, EasResponse, import_all
from .devices import Devices
from .request import NAMES, RequestError, read_request
from .wakeup import Waker
from .wbxml import Limits, WbxmlError

Services = Any

WBXML_TYPE: Final = "application/vnd.ms-sync.wbxml"
VERSION: Final = "14.1"
CONCURRENT_PER_ACCOUNT: Final = 8
HEADER: Final = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "MS-Server-ActiveSync": VERSION}
_AGENT: Final = re.compile(r"[A-Za-z][A-Za-z0-9._\-]{0,40}(/[0-9][A-Za-z0-9._\-]{0,20})?")
_HEADER_VALUES: Final = frozenset({"ms-asprotocolversion", "content-type", "accept-encoding"})
_HEADER_FORBIDDEN: Final = frozenset({"authorization", "cookie", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip", "x-forwarded-port", "x-forwarded-server", "forwarded"})


@dataclass(slots=True)
class EasServices:
    waker: Waker
    devices: Devices
    simultaneously: dict[str, int] = field(default_factory=dict)


def build(store, clock) -> EasServices:
    import_all()
    return EasServices(Waker(clock), Devices(store))


def is_waiting(command: str, payload: wbxml.Node | None) -> bool:
    """Ping and Sync with Wait/HeartbeatInterval wait (no account slot); a Sync with client Commands never waits and always counts."""
    if command == "Ping":
        return True
    if command != "Sync" or payload is None:
        return False
    if tags.find(payload, "AirSync:Wait") is None and tags.find(payload, "AirSync:HeartbeatInterval") is None:
        return False
    collection = tags.find(payload, "AirSync:Collections")
    return not any(tags.find(c, "AirSync:Commands") is not None for c in (tags.find_all(collection, "AirSync:Collection") if collection is not None else ()))


def _agent(request: Request) -> str | None:
    raw = (request.headers.get("user-agent") or "").split(" ", 1)[0]
    m = _AGENT.match(raw)
    return m.group(0) if m else None


def _empty(status: int, header: dict | None = None) -> Response:
    return Response(status_code=status, headers={**HEADER, **(header or {})})


def _unauthorized(d: "Services") -> Response:
    host = d.config.public_url.split("://", 1)[-1]
    return Response(status_code=401, headers={**HEADER, "WWW-Authenticate": f'Basic realm="{host}"'})


async def _read(request: Request, limit: int) -> bytes | None:
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > limit:
        return None
    parts, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        parts.append(chunk)
    return b"".join(parts)


def _measurement(d: "Services", request: Request, command: str | None, request_skeleton: list[str] | None, response_skeleton: list[str] | None, status: int, codes: tuple[str, ...]) -> None:
    """Structure without content; header lines only as names (value only for an allowlist), never Authorization/Cookie/X-Forwarded-*."""
    try:
        moment = datetime.fromtimestamp(d.clock.now(), timezone.utc)
        headers = []
        for name in sorted({k.lower() for k in request.headers.keys()} - _HEADER_FORBIDDEN)[:40]:
            if name in _HEADER_VALUES:
                value = (request.headers.get(name) or "")[:40]
                headers.append(f"{name}={value}" if re.fullmatch(r"[A-Za-z0-9.;=/\- ,]{0,40}", value) else name)
            elif name == "user-agent":
                headers.append(f"user-agent={_agent(request) or '-'}")
            elif re.fullmatch(r"[a-z0-9\-]{1,40}", name):
                headers.append(name)
        line = {
            "time": moment.isoformat(timespec="milliseconds"),
            "command": command,
            "status": status,
            "codes": list(codes)[:20],
            "header": headers,
            "request": request_skeleton or [],
            "response": response_skeleton or [],
        }
        d.storage.write((d.config.logs_dir or d.config.state_dir) / "capture" / f"eas-{moment:%Y-%m-%d}.jsonl", json.dumps(line, ensure_ascii=True))
    except Exception as e:  # noqa: BLE001
        d.log.error("eas_capture_exception", e)


async def handle(http_request: Request, d: "Services") -> Response:
    start = time.monotonic()
    eas: EasServices = d.eas  # type: ignore[assignment]
    method = http_request.method.upper()
    if method not in ("OPTIONS", "POST", "GET"):
        return _empty(405, {"Allow": "OPTIONS,POST"})
    ip = http_request.client.host if http_request.client else "unknown"
    try:
        session = await d.login.check(http_request.headers.get("authorization"), ip, namespace="eas:", account_throttle=True)
    except LoginRejected as e:
        d.log.event("login_rejected", reason=e.reason, operation="eas")
        if e.reason == "locked":
            return _empty(503, {"Retry-After": str(max(1, min(e.rest_s, 900)))})
        if e.reason == "caldav-unavailable":
            return _empty(503, {"Retry-After": "30"})
        if e.reason == "no-default-calendar":
            return _empty(403)
        return _unauthorized(d)
    d.notifier.watch(session.account)
    if not session.account.eas:
        return PlainTextResponse("user is not enabled for ActiveSync", status_code=403, headers=HEADER)
    try:
        request = read_request(method, http_request.url.query, dict(http_request.headers))
    except RequestError as e:
        d.log.event("eas_request", command=None, status=e.status, account=session.account.name, http_status=e.status)
        return _empty(e.status)
    if request.method == "OPTIONS":
        return _empty(200, {"MS-ASProtocolVersions": VERSION, "MS-ASProtocolCommands": ",".join(ADVERTISED), "Allow": "OPTIONS,POST", "Public": "OPTIONS,POST"})
    if request.method == "GET":
        return PlainTextResponse("Exchange ActiveSync facade for CalDAV calendars.", headers=HEADER)
    agent = _agent(http_request)
    command = request.command
    if request.version != VERSION:
        d.log.event("version_rejected", version=request.version if request.version and re.fullmatch(r"[0-9.]{1,6}", request.version) else None, agent=agent, command=command)
        return _empty(400)
    if command is None or command not in NAMES:
        d.log.event("eas_command_unknown", value=request.command_code, agent=agent)
        return _empty(400)
    if command not in ADVERTISED:
        d.log.event("eas_command_unknown", command=command, agent=agent)
        return _empty(501)
    account = session.account.name
    device = d.ids.device_hash(account, request.device or "")
    outcome = await d.store.eas_device_see(account, device, request.device_type or "", agent, d.config.eas.device_limit)
    if outcome == "displaced":
        d.log.event("eas_device_replaced", account=account, device=device, device_type=request.device_type)
    if outcome in ("new", "displaced"):
        d.log.event("eas_device_new", account=account, device=device, device_type=request.device_type, agent=agent)
    raw = await _read(http_request, d.config.eas.request_body_max_bytes)
    if raw is None:
        return _empty(413)
    payload = None
    if raw:
        type = (http_request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if type not in (WBXML_TYPE, "application/vnd.ms-sync"):
            return _empty(400)
        try:
            payload = wbxml.decode(raw, Limits(max_bytes=d.config.eas.request_body_max_bytes))
        except WbxmlError as e:
            d.log.event("eas_wbxml_error", reason=e.reason, command=command, device=device)
            return _empty(400)
    elif command not in ("Sync", "Ping"):
        return _empty(400)
    waiting = is_waiting(command, payload)
    if not waiting:
        if eas.simultaneously.get(account, 0) >= CONCURRENT_PER_ACCOUNT:
            return _empty(503, {"Retry-After": "5"})
        eas.simultaneously[account] = eas.simultaneously.get(account, 0) + 1
    request_id = uuid.uuid4().hex[:12]
    ctx = EasContext(
        session, request, payload, hashlib.sha256(raw).digest(), d.config, d.ids, d.store, d.caldav, d.info, d.locks, d.notifier, eas.waker, eas.devices, d.clock, d.log, request_id, d.end, device, raw
    )
    result: EasResponse
    http_status = 200
    try:
        handler = COMMANDS[command]
        if waiting:
            result = await handler(ctx)
        else:
            result = await asyncio.wait_for(handler(ctx), timeout=d.config.eas.request_max_seconds)
    except asyncio.TimeoutError:
        result = EasResponse(None, 503, ("time",), {"Retry-After": "30"})
    except CaldavError as e:
        if e.kind == CaldavKind.AUTH:
            d.login.forget(account, session.credentials)
            d.info.discard(account)
            return _unauthorized(d)
        d.log.event("eas_caldav_error", caldav_kind=e.kind.value, command=command, request_id=request_id)
        result = EasResponse(None, 503, ("caldav",), {"Retry-After": "30"})
    except Exception as e:  # noqa: BLE001 - only the type name is logged
        d.log.error("eas_exception", e, command=command, request_id=request_id)
        result = EasResponse(None, 500, ("exception",))
    finally:
        if not waiting:
            eas.simultaneously[account] = max(0, eas.simultaneously.get(account, 1) - 1)
    content = b""
    if result.root is not None:
        try:
            content = wbxml.encode(result.root)
        except (WbxmlError, KeyError) as e:
            d.log.error("eas_encode_exception", e, command=command, request_id=request_id)
            result = EasResponse(None, 500, ("encode",))
            content = b""
    http_status = result.http_status
    duration_ms = int((time.monotonic() - start) * 1000)
    d.log.event(
        "eas_request",
        command=command,
        version=request.version,
        account=account,
        device=device,
        device_type=request.device_type,
        agent=agent,
        http_status=http_status,
        codes=[c for c in result.codes if re.fullmatch(r"[A-Za-z0-9]{1,12}", c)][:20],
        count=result.count,
        bytes_in=len(raw),
        bytes_out=len(content),
        duration_ms=duration_ms,
        request_id=request_id,
    )
    if session.account.test and session.account.capture:
        _measurement(
            d,
            http_request,
            command,
            skeleton_module.skeleton(payload) if payload is not None else None,
            skeleton_module.skeleton(result.root) if result.root is not None else None,
            http_status,
            result.codes,
        )
    if not content:
        return _empty(http_status, dict(result.header))
    return Response(content, status_code=http_status, media_type=WBXML_TYPE, headers={**HEADER, **result.header})
