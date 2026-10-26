"""The FastAPI application: wiring, security middleware, error shapes and static files."""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hearthkeep import db
from hearthkeep.agent.loop import AgentLoop
from hearthkeep.agent.runner import TurnRunner, recover_after_restart
from hearthkeep.api import (
    routes_approvals,
    routes_audit,
    routes_auth,
    routes_chats,
    routes_images,
    routes_models,
    routes_settings,
)
from hearthkeep.api.common import (
    CSRF_HEADER,
    ApiError,
    HealthCheck,
    RateLimiter,
    Services,
    error_response,
    services,
)
from hearthkeep.approvals import ApprovalStore
from hearthkeep.auth.credentials import CredentialStore
from hearthkeep.auth.enrollment import EnrollmentCodes
from hearthkeep.auth.sessions import SessionStore, StepUp
from hearthkeep.auth.webauthn import ChallengeStore, Verifier
from hearthkeep.config import LOOPBACK_HOSTS, Settings, detect_docker
from hearthkeep.images import ImageStore
from hearthkeep.runtime.stack import ModelStack
from hearthkeep.secrets import SecretStore
from hearthkeep.tools.base import ToolRegistry

DB_FILE = "hearthkeep.db"
DEFAULT_WEB_DIST = Path(__file__).resolve().parents[4] / "web" / "dist"
HEALTH_TIMEOUT_SECONDS = 2.0
SSE_KEEPALIVE_SECONDS = 15.0

SECURITY_HEADERS = (
    (
        b"content-security-policy",
        b"default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        b"connect-src 'self'; frame-ancestors 'none'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def chat_model_health_check(settings: Settings) -> HealthCheck:
    """A quick reachability probe of the chat model server; never raises."""
    url = settings.chat_base_url.rstrip("/") + "/models"

    async def check() -> bool:
        try:
            # trust_env=False: a local probe never goes through a system proxy.
            async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT_SECONDS, trust_env=False) as http:
                response = await http.get(url)
        except Exception:
            return False
        return response.status_code < 500

    return check


def _host_name(value: str) -> str | None:
    """The host name of a Host header value (port and IPv6 brackets dropped), lowercased."""
    try:
        return urlsplit("//" + value).hostname
    except ValueError:
        return None


class SecurityMiddleware:
    """Pure ASGI (safe for streaming): security headers on every response, a Host allowlist
    (the public_origin host and loopback names, any port: blocks DNS rebinding), and CSRF
    checks on every state-changing request (`X-Hearthkeep: 1` plus an exact `Origin`)."""

    def __init__(self, app: ASGIApp, public_origin: str) -> None:
        self.app = app
        self.origin = public_origin.rstrip("/")
        self.hosts = LOOPBACK_HOSTS | {urlsplit(self.origin).hostname}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False
        ended = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started, ended
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                ended = True
            if message["type"] == "http.response.start":
                started = True
                names = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(n, v) for n, v in SECURITY_HEADERS if n not in names]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        if not self._host_ok(scope):
            await error_response(400, "bad_host")(scope, receive, send_with_headers)
            return
        if scope["method"] in UNSAFE_METHODS and not self._csrf_ok(scope):
            response = error_response(403, "csrf")
            await response(scope, receive, send_with_headers)
            return
        try:
            await self.app(scope, receive, send_with_headers)
        except Exception as exc:
            # Only the type, never the message or a traceback: they may carry private data.
            # Never re-raised, so Starlette's ServerErrorMiddleware never logs it either.
            _log.error(
                "unhandled error in %s %s: %s", scope["method"], scope["path"], type(exc).__name__
            )
            if not started:
                await error_response(500, "internal_error")(scope, receive, send_with_headers)
            elif not ended:
                # Headers are out: all we can do is end the body; the client sees a short stream.
                try:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                except Exception as send_exc:
                    _log.error("could not end response: %s", type(send_exc).__name__)

    def _host_ok(self, scope: Scope) -> bool:
        hosts = [v for n, v in scope.get("headers", []) if n.lower() == b"host"]
        if len(hosts) != 1:
            return False
        return _host_name(hosts[0].decode("latin-1")) in self.hosts

    def _csrf_ok(self, scope: Scope) -> bool:
        headers = {}
        for name, value in scope.get("headers", []):
            headers.setdefault(name.decode("latin-1").lower(), value.decode("latin-1"))
        return headers.get(CSRF_HEADER) == "1" and headers.get("origin") == self.origin


_REASON_FOR_STATUS = {404: "not_found", 405: "method_not_allowed"}


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.reason, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            {"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", ""))}
            for e in exc.errors()
        ]
        return error_response(422, "invalid_request", problems=problems)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        reason = _REASON_FOR_STATUS.get(exc.status_code, "http_error")
        return JSONResponse(
            {"reason": reason}, status_code=exc.status_code, headers=exc.headers or None
        )


def _install_static(app: FastAPI, web_dist: Path) -> None:
    root = web_dist.resolve()
    index = root / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path == "api" or path.startswith("api/"):
            return error_response(404, "not_found")
        if path:
            candidate = (root / path).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return error_response(404, "not_found")


def create_app(
    settings: Settings,
    *,
    verifier: Verifier,
    chat_client,
    registry: ToolRegistry,
    clock: Callable[[], datetime] = _utcnow,
    health_check: HealthCheck | None = None,
    web_dist: Path | None = DEFAULT_WEB_DIST,
    secrets: SecretStore | None = None,
    rate_limiter: RateLimiter | None = None,
    sse_keepalive: float = SSE_KEEPALIVE_SECONDS,
    approval_poll_interval: float = 0.5,
    monotonic: Callable[[], float] = time.monotonic,
    models: ModelStack | None = None,
) -> FastAPI:
    if settings.docker_available is None:
        settings = dataclasses.replace(settings, docker_available=detect_docker())
    check = health_check or chat_model_health_check(settings)
    stack = models or ModelStack()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The connection is opened on the event loop's thread; every handler is async and
        # runs there, so it is only ever used from one thread.
        conn = db.connect(Path(settings.data_dir) / DB_FILE)
        recover_after_restart(conn)
        approvals = ApprovalStore(
            conn, settings.approval_ttl_minutes, clock, poll_interval=approval_poll_interval
        )
        gpu = await stack.build_gpu()
        images = ImageStore(conn, settings.data_dir)
        image_tool = stack.image_tool(gpu, images)
        if image_tool is not None and registry.get(image_tool.name) is None:
            registry.register(image_tool)
        chat = stack.chat_client(chat_client, gpu)
        loop = AgentLoop(chat, registry, approvals, conn, settings, secrets=secrets)
        runner = TurnRunner(conn, loop)
        sessions = SessionStore(conn, settings.session_idle_days, clock=clock)
        app.state.hk = Services(
            settings=settings,
            conn=conn,
            verifier=verifier,
            registry=registry,
            clock=clock,
            challenges=ChallengeStore(clock=clock),
            sessions=sessions,
            step_up=StepUp(clock=clock),
            codes=EnrollmentCodes(conn, clock=clock),
            credentials=CredentialStore(conn, clock=clock),
            approvals=approvals,
            runner=runner,
            limiter=rate_limiter or RateLimiter(),
            health_check=check,
            sse_keepalive=sse_keepalive,
            gpu=gpu,
            images=images,
            cards=dict(stack.cards),
            card_errors=list(stack.card_errors),
            managed=stack.managed,
            monotonic=monotonic,
        )
        try:
            yield
        finally:
            try:
                await runner.shutdown()
            finally:
                # After the runner: no turn can still be holding a lease on a model server.
                await stack.stop()
                conn.close()

    app = FastAPI(
        title="Hearthkeep",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    _install_error_handlers(app)

    @app.get("/api/health")
    async def health(request: Request) -> dict:
        return {
            "status": "ok",
            "chat_model": settings.chat_model,
            "chat_model_reachable": await services(request).chat_model_reachable(),
        }

    for module in (
        routes_auth,
        routes_chats,
        routes_approvals,
        routes_settings,
        routes_audit,
        routes_models,
        routes_images,
    ):
        app.include_router(module.router)

    if web_dist is not None and Path(web_dist).is_dir():
        _install_static(app, Path(web_dist))

    app.add_middleware(SecurityMiddleware, public_origin=settings.public_origin)
    return app
