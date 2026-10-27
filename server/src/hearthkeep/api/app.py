"""The FastAPI application: wiring, security middleware, error shapes and static files."""

from __future__ import annotations

import dataclasses
import logging
import mimetypes
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
    routes_files,
    routes_folders,
    routes_images,
    routes_models,
    routes_push,
    routes_runs,
    routes_sent,
    routes_settings,
    routes_uploads,
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
from hearthkeep.files.store import ChatFileStore
from hearthkeep.folders import FolderStore
from hearthkeep.images import ImageStore
from hearthkeep.numbercheck import NumberChecker
from hearthkeep.push import PushSender, PushSubscriptions, VapidKeys
from hearthkeep.runtime.stack import ModelStack
from hearthkeep.sandbox import image as sandbox_image
from hearthkeep.sandbox.handle import SandboxHandle
from hearthkeep.sandbox.inspect import Inspector
from hearthkeep.sandbox.runner import SandboxRunner
from hearthkeep.sandbox.runs import RunStore
from hearthkeep.secrets import SecretStore, SecretStoreError
from hearthkeep.sent import SentStore
from hearthkeep.tools.base import ToolRegistry
from hearthkeep.tools.files import FilesTool
from hearthkeep.tools.python import PythonTool, analysis_context
from hearthkeep.tools.read_pages import ReadPagesTool
from hearthkeep.tools.send import SendToDevicesTool

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


def _push_sender(store: SecretStore) -> PushSender | None:
    """The push sender, its VAPID key made on first start; None (push answers 503) when the
    secret store or the keyring can't be used or the stored key is corrupt, so none of that
    ever stops Hearthkeep from starting. Only the error type is logged, never the key."""
    try:
        return PushSender(VapidKeys.load_or_create(store))
    except (SecretStoreError, ValueError, TypeError) as exc:
        _log.warning("push is off: the VAPID key can't be loaded (%s)", type(exc).__name__)
        return None


async def _make_sandbox(settings: Settings) -> SandboxRunner:
    """A sandbox runner; containers a stopped Hearthkeep left behind are removed first (none of
    this runner's can exist yet)."""
    sandbox = SandboxRunner(Path(settings.data_dir), image_tag=sandbox_image.image_tag())
    try:
        await sandbox.sweep()
    except Exception as exc:
        _log.warning("could not sweep sandbox containers: %s", type(exc).__name__)
    return sandbox


async def _sandbox_handle(
    settings: Settings, given: SandboxRunner | None, detected: bool
) -> SandboxHandle:
    """The runner given (tests); else one made now when Docker is running; else, when Docker
    was looked for and missing, one made the first time it is seen up. Docker configured off
    (docker_available = false) never gets one."""
    if given is not None:
        return SandboxHandle(given)
    if settings.docker_available:
        return SandboxHandle(await _make_sandbox(settings))
    if detected:
        return SandboxHandle(
            factory=lambda: _make_sandbox(settings), docker_check=sandbox_image.docker_ok
        )
    return SandboxHandle()


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
    sandbox: SandboxRunner | None = None,
    push_sender: PushSender | None = None,
) -> FastAPI:
    """`sandbox`, when given (tests), is used as is; otherwise one is made when Docker is
    available. Without one the `python` tool is still offered and answers that Docker is
    needed: nothing ever runs on the host. `push_sender`, when given (tests: a fake, or one
    on a mock transport), is used instead of the real one, so tests never touch the network."""
    # The static files' types come from `mimetypes`, which on Windows reads the registry: a
    # .js mapped to text/plain there would stop the service worker from registering.
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/javascript", ".mjs")
    detected = settings.docker_available is None
    if detected:
        settings = dataclasses.replace(settings, docker_available=detect_docker())
    check = health_check or chat_model_health_check(settings)
    stack = models or ModelStack()
    store = secrets or SecretStore(Path(settings.data_dir))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The connection is opened on the event loop's thread; every handler is async and
        # runs there, so it is only ever used from one thread.
        conn = db.connect(Path(settings.data_dir) / DB_FILE)
        runner: TurnRunner | None = None
        push: PushSender | None = None
        sent: SentStore | None = None
        try:
            recover_after_restart(conn)
            approvals = ApprovalStore(
                conn, settings.approval_ttl_minutes, clock, poll_interval=approval_poll_interval
            )
            gpu = await stack.build_gpu()
            images = ImageStore(conn, settings.data_dir)
            files = ChatFileStore(conn, settings.data_dir)
            # Uploads cut off by a stop leave temporary files behind; none can be live yet.
            files.sweep_temp()
            # Inspections a stop cut short: those files couldn't be checked.
            files.settle_pending()
            handle = await _sandbox_handle(settings, sandbox, detected)
            runs = RunStore(conn, settings.data_dir)
            image_tool = stack.image_tool(gpu, images)
            if image_tool is not None and registry.get(image_tool.name) is None:
                registry.register(image_tool)
            if registry.get(PythonTool.name) is None:
                registry.register(PythonTool(handle, files, runs))
            # The chat client the turns use (the scheduled one when Hearthkeep runs the model):
            # read_pages asks the same model to read each page, with its own GPU lease.
            chat = stack.chat_client(chat_client, gpu)
            if registry.get(ReadPagesTool.name) is None:
                registry.register(ReadPagesTool(handle, files, runs, chat))
            folders = FolderStore(conn, settings.data_dir)
            inspector = Inspector(handle, files)
            if registry.get(FilesTool.name) is None:
                registry.register(FilesTool(folders, files, inspector))
            checker = NumberChecker(conn, runs, files)
            credentials = CredentialStore(conn, clock=clock)
            push = push_sender if push_sender is not None else _push_sender(store)
            subscriptions = PushSubscriptions(conn)
            sent = SentStore(conn, runs, files, credentials, checker, push, subscriptions)
            if registry.get(SendToDevicesTool.name) is None:
                registry.register(SendToDevicesTool(sent, credentials))
            loop = AgentLoop(
                chat,
                registry,
                approvals,
                conn,
                settings,
                secrets=store,
                context=lambda chat_id: analysis_context(files, chat_id),
                docker=handle.docker_available,
                reply_checker=checker,
            )
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
                credentials=credentials,
                approvals=approvals,
                runner=runner,
                limiter=rate_limiter or RateLimiter(),
                health_check=check,
                sse_keepalive=sse_keepalive,
                gpu=gpu,
                images=images,
                files=files,
                cards=dict(stack.cards),
                card_errors=list(stack.card_errors),
                managed=stack.managed,
                chat_model_id=stack.chat_model_id,
                monotonic=monotonic,
                sandbox=handle,
                runs=runs,
                inspector=inspector,
                checker=checker,
                folders=folders,
                push=push,
                subscriptions=subscriptions,
                sent=sent,
            )
            yield
        finally:
            # Also runs when setup above fails part way: the connection is always closed.
            try:
                if runner is not None:
                    await runner.shutdown()
            finally:
                try:
                    # After the runner (no turn can still start a push): pushes still going
                    # are given a moment, then cancelled, before the sender is closed.
                    try:
                        if sent is not None:
                            await sent.aclose()
                    finally:
                        if push is not None:
                            await push.aclose()
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
            **await services(request).chat_model_status(),
        }

    for module in (
        routes_auth,
        routes_chats,
        routes_approvals,
        routes_settings,
        routes_audit,
        routes_models,
        routes_images,
        routes_uploads,
        routes_files,
        routes_folders,
        routes_runs,
        routes_push,
        routes_sent,
    ):
        app.include_router(module.router)

    if web_dist is not None and Path(web_dist).is_dir():
        _install_static(app, Path(web_dist))

    app.add_middleware(SecurityMiddleware, public_origin=settings.public_origin)
    return app
