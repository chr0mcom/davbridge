"""ASGI app: routes, lifespan, dispatch and error frame. One worker, no BaseHTTPMiddleware, no compression."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Callable

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__, protocol_capture, soap
from .auth import Login, LoginRejected
from .caldav import Caldav, CaldavError, CaldavKind, HttpCaldav
from .calendar_info import CalendarInfoCache
from .clock import Clock, SystemClock
from .config import Config, check_start, config_path, load, read_key, set_runtime_default_zone
from .eas import http as eas_http
from .eas import mobile_autodiscover
from .eas import timezone as eas_timezone
from .errors import Code, EwsError, from_caldav
from .event_log import EventLog, FileStorage
from .identifiers import IdService
from .locks import Locks
from .notifier import Notifier
from .operations import OPERATIONS, Context, Messages, Reply, Stream, autodiscover, import_all
from .proxy_headers import ProxyHeaders
from .store import Store
from .subscriptions import SubscriptionManagement

MAX_BODY = soap.MAX_BODY
XML = "text/xml; charset=utf-8"
HEADER = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


@dataclass(slots=True)
class Services:
    config: Config
    ids: IdService
    store: Store
    caldav: Caldav
    http: httpx.AsyncClient | None
    login: Login
    subscriptions: SubscriptionManagement
    notifier: Notifier
    locks: Locks
    info: CalendarInfoCache
    clock: Clock
    log: EventLog
    capture: protocol_capture.ProtocolCapture
    storage: FileStorage
    end: asyncio.Event
    started: float
    eas: "eas_http.EasServices | None" = None


async def build_services(config: Config, key: bytes, clock: Clock, caldav: Caldav | None = None) -> Services:
    set_runtime_default_zone(config.default_timezone)
    http = None
    if caldav is None:
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(config.read_timeout_seconds, connect=config.connect_timeout_seconds),
            limits=httpx.Limits(max_connections=10),
            follow_redirects=config.follow_redirects,
            verify=config.verify_tls,
        )
        caldav = HttpCaldav(config.caldav_base_url, http)
    ids = IdService(key)
    store = await Store.open(config.state_dir / "davbridge.sqlite", clock, config.failure_limit, config.initial_lock_seconds)
    storage = FileStorage(config.logs_dir, now=clock.now)
    log = EventLog(config.logs_dir, clock, storage, level=config.log_level, stdout=config.log_stdout)
    subscriptions = SubscriptionManagement(ids, clock)
    login = Login(config, caldav, store, ids, clock)
    eas = None
    if config.eas.enabled:
        store.set_eas_expiry(config.eas.device_expiry_days)
        eas = eas_http.build(store, clock)
        asyncio.get_running_loop().run_in_executor(None, eas_timezone.warm_up)
    notifier = Notifier(config, caldav, subscriptions, login, store, clock, log, ids, eas.waker if eas is not None else None)
    return Services(
        config,
        ids,
        store,
        caldav,
        http,
        login,
        subscriptions,
        notifier,
        Locks(),
        CalendarInfoCache(caldav, clock),
        clock,
        log,
        protocol_capture.ProtocolCapture((config.logs_dir or config.state_dir) / "capture", clock, storage),
        storage,
        asyncio.Event(),
        clock.now(),
        eas,
    )


def _raw(d: "Services", request: Request, classification: str, status: int) -> None:
    """Rejected requests also appear in the log (path class, status, login scheme without value). Unknown paths with path and parameter names."""
    schema = (request.headers.get("authorization") or "").partition(" ")[0]
    schema = schema if schema in ("Basic", "NTLM", "Negotiate", "Bearer") else ("none" if not schema else "other")
    extra = {}
    if classification == "other_path":
        extra = {"path": mobile_autodiscover.path_segments(request.url.path), "query": mobile_autodiscover.query_names(request.url.query) or None}
    d.log.event("raw_request", operation=classification, status=status, schema=schema, value=1 if request.method == "POST" else 0, **extra)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _response(content: bytes, status: int = 200) -> Response:
    return Response(content, status_code=status, media_type=XML, headers=HEADER)


def _unauthorized(config: Config) -> Response:
    host = config.public_url.split("://", 1)[-1]
    return Response(status_code=401, headers={**HEADER, "WWW-Authenticate": f'Basic realm="{host}"'})


async def _read_body(request: Request) -> bytes | None:
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY:
        return None
    parts, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY:
            return None
        parts.append(chunk)
    return b"".join(parts)


def _write_status(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _end_on_signal(d: Services) -> None:
    """uvicorn first waits for open responses after SIGTERM and only then runs the lifespan shutdown. Open streams must send `Closed` before that."""
    loop = asyncio.get_running_loop()

    def report() -> None:
        d.end.set()
        d.subscriptions.end()

    for number in (signal.SIGTERM, signal.SIGINT):
        old = signal.getsignal(number)

        def handle(num, frame, old=old) -> None:
            loop.call_soon_threadsafe(report)
            if callable(old):
                old(num, frame)

        try:
            signal.signal(number, handle)
        except ValueError:
            return


async def _status_writer(d: Services) -> None:
    path = d.config.state_dir / "status.json"
    while not d.end.is_set():
        try:
            now = d.clock.now()
            content = {
                "time": int(now),
                "version": __version__,
                "commit": d.config.commit,
                "subscriptions": len(d.subscriptions.all()),
                "streams": sum(1 for a in d.subscriptions.all() if a.connection is not None),
                "last_caldav_ok": d.notifier.last_caldav_ok(),
                "caldav_errors_since": d.notifier.caldav_error_since(),
                "active_locks": await d.store.locks_active(),
                "log_fields_dropped": d.log.discarded,
            }
            if d.eas is not None:
                content["eas_devices"] = await d.store.eas_count_devices()
                content["eas_pings"] = d.eas.waker.count()
            await asyncio.to_thread(_write_status, path, json.dumps(content))
        except Exception as e:  # noqa: BLE001
            d.log.error("status_exception", e)
        await asyncio.sleep(60)


async def _cleaner(d: Services) -> None:
    while not d.end.is_set():
        await asyncio.sleep(60)
        try:
            await d.store.clean_up()
            d.subscriptions.clean_up()
            keep = {a.account for a in d.subscriptions.all()} | (set(d.eas.waker.accounts()) if d.eas is not None else set())
            d.login.clean_up(keep=keep)
        except Exception as e:  # noqa: BLE001
            d.log.error("cleanup_exception", e)


def create_app(services_factory: Callable[[], "asyncio.Future[Services]"] | None = None) -> Starlette:
    import_all()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if services_factory is not None:
            d = await services_factory()
        else:
            config = load(config_path(os.environ), os.environ)
            check_start(config)
            key = read_key(config.state_dir / "hmac.key")
            d = await build_services(config, key, SystemClock())
        app.state.d = d
        _end_on_signal(d)
        tasks = [asyncio.create_task(d.notifier.run(d.end)), asyncio.create_task(_status_writer(d)), asyncio.create_task(_cleaner(d))]
        try:
            yield
        finally:
            d.end.set()
            d.subscriptions.end()
            if d.eas is not None:
                d.eas.waker.end()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if d.http is not None:
                await d.http.aclose()
            await d.store.close()
            d.storage.close()
            try:
                os.unlink(d.config.state_dir / "status.json")
            except OSError:
                pass

    async def endpoint(request: Request) -> Response:
        d: Services = request.app.state.d
        path = request.url.path.lower().rstrip("/")
        if request.method == "OPTIONS" and path != "/microsoft-server-activesync":
            return Response(status_code=405, headers={**HEADER, "Allow": "GET, POST"})
        if path == "/healthz" and request.method == "GET":
            ok = await d.store.healthy()
            since = d.notifier.caldav_error_since()
            if d.subscriptions.all() and since is not None and d.clock.now() - since > 300:
                ok = False
            return PlainTextResponse("ok" if ok else "error", status_code=200 if ok else 503, headers=HEADER)
        if path == "/ews/exchange.asmx" and d.config.ews_enabled:
            response = await _ews(request, d)
            _raw(d, request, "ews", response.status_code)
            return response
        if path == "/autodiscover/autodiscover.xml" and (d.config.ews_enabled or d.eas is not None):
            response = await _autodiscover(request, d)
            _raw(d, request, "autodiscover", response.status_code)
            return response
        if d.eas is not None and (path == "/autodiscover/autodiscover.json" or path.startswith("/autodiscover/autodiscover.json/")):
            status, content, value = mobile_autodiscover.json_response(d.config, request.url.path, request.url.query)
            d.log.event("mobile_autodiscover", kind_mobile="json", protocol_value=value, status=status)
            return Response(content, status_code=status, media_type="application/json", headers=HEADER)
        if d.eas is not None and path == "/microsoft-server-activesync":
            return await eas_http.handle(request, d)
        if path.startswith(("/ews", "/autodiscover")):
            _raw(d, request, "other_path", 404)
        return PlainTextResponse("", status_code=404, headers=HEADER)

    async def eas_endpoint(request: Request) -> Response:
        d: Services = request.app.state.d
        if d.eas is None:
            return PlainTextResponse("", status_code=404, headers=HEADER)
        return await eas_http.handle(request, d)

    routes = [Route(p, eas_endpoint, methods=["GET", "POST", "OPTIONS"]) for p in ("/Microsoft-Server-ActiveSync", "/microsoft-server-activesync")]
    return Starlette(routes=[*routes, Route("/{path:path}", endpoint, methods=["GET", "POST", "OPTIONS"])], lifespan=lifespan, middleware=[Middleware(ProxyHeaders)])


async def _sign_in(request: Request, d: Services):
    try:
        session = await d.login.check(request.headers.get("authorization"), _client_ip(request))
    except LoginRejected as e:
        d.log.event("login_rejected", reason=e.reason)
        if e.reason == "caldav-unavailable":
            return None, Response(status_code=503, headers={**HEADER, "Retry-After": "30"})
        if e.reason == "no-default-calendar":
            return None, Response(status_code=403, headers=HEADER)
        return None, _unauthorized(d.config)
    d.notifier.watch(session.account)
    return session, None


async def _ews(http_request: Request, d: Services) -> Response:
    session, rejected = await _sign_in(http_request, d)
    if rejected is not None:
        return rejected
    if http_request.method == "GET":
        return PlainTextResponse("DAVbridge calendar bridge for CalDAV. Not a full Exchange server.", headers=HEADER)
    body = await _read_body(http_request)
    if body is None:
        return Response(status_code=413, headers=HEADER)
    start = time.monotonic()
    try:
        request = soap.read(body, http_request.headers.get("soapaction"))
    except soap.SoapError as e:
        return _response(soap.fault(e.code), 500)
    operation = request.operation
    ctx = Context(
        session,
        request,
        d.config,
        d.ids,
        d.store,
        d.caldav,
        d.subscriptions,
        d.notifier,
        d.locks,
        d.info,
        d.clock,
        d.log,
        uuid.uuid4().hex[:12],
        d.end,
        http_request.headers.get("user-agent", "").startswith(("MacOutlook", "Microsoft Office")),
    )
    skeleton = None
    if session.account.test and session.account.capture:
        try:
            skeleton = protocol_capture.skeleton(request, dict(http_request.headers), datetime.fromtimestamp(d.clock.now(), timezone.utc))
        except Exception as e:  # noqa: BLE001
            d.log.error("capture_exception", e)
    if request.impersonation:
        return _response(soap.response_error(operation, Code.ErrorImpersonationDenied))
    handler = OPERATIONS.get(operation)
    if handler is None:
        result = Messages((soap.message(operation, Code.ErrorInvalidRequest),), (Code.ErrorInvalidRequest.value,))
    else:
        try:
            result = await handler(ctx)
        except EwsError as e:
            result = Messages((soap.message(operation, e.code, backoff_ms=e.backoff_ms),), (e.code.value,))
        except CaldavError as e:
            if e.kind == CaldavKind.AUTH:
                d.login.forget(session.account.name)
                d.info.discard(session.account.name)
                return _unauthorized(d.config)
            f = from_caldav(e, writing=False)
            result = Messages((soap.message(operation, f.code, backoff_ms=f.backoff_ms),), (f.code.value,))
        except Exception as e:  # noqa: BLE001 - only the type name is logged
            d.log.error("operation_exception", e, operation=operation, request_id=ctx.request_id)
            result = Messages((soap.message(operation, Code.ErrorInternalServerError),), (Code.ErrorInternalServerError.value,))
    duration_ms = int((time.monotonic() - start) * 1000)
    if isinstance(result, Stream):
        d.log.request(operation, session.account.name, ("NoError",), 1, duration_ms, request_id=ctx.request_id)
        if skeleton is not None:
            await d.capture.capture(session.account, skeleton, ("NoError",), duration_ms)
        return StreamingResponse(result.chunks, media_type=XML, headers={**HEADER, "X-Accel-Buffering": "no"})
    if isinstance(result, Reply):
        content = soap.response_with_root(result.root)
        codes = result.codes
    else:
        content = soap.response(operation, list(result.messages))
        codes = result.codes
    d.log.request(operation, session.account.name, codes, len(codes), duration_ms, request_id=ctx.request_id)
    if skeleton is not None:
        await d.capture.capture(session.account, skeleton, codes, duration_ms)
    return _response(content)


async def _autodiscover(request: Request, d: Services) -> Response:
    length = request.headers.get("content-length", "")
    if d.eas is not None and request.method == "POST" and length.isdigit() and 0 < int(length) <= mobile_autodiscover.MAX_POX:
        body = await _read_body(request) or b""
        if mobile_autodiscover.is_mobilesync(body):
            d.log.event("mobile_autodiscover", kind_mobile="pox", status=200)
            return _response(mobile_autodiscover.pox_response(d.config, body))
        request._body = body  # body already read; _read_body below reads it again from the buffer
    if not d.config.ews_enabled:
        return PlainTextResponse("", status_code=404, headers=HEADER)
    session, rejected = await _sign_in(request, d)
    if rejected is not None:
        return rejected
    address = autodiscover.requested_address(await _read_body(request) or b"")
    if address is not None and address != session.account.address.lower():
        return _response(autodiscover.error())
    return _response(autodiscover.response(d.config, session.account))


app = create_app()
