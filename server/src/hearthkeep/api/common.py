"""Shared pieces of the HTTP API: the service container, errors, session and rate limiting."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse

from hearthkeep import audit
from hearthkeep.agent.runner import TurnRunner
from hearthkeep.approvals import ApprovalStore
from hearthkeep.auth.credentials import CredentialStore
from hearthkeep.auth.enrollment import EnrollmentCodes
from hearthkeep.auth.sessions import Session, SessionStore, StepUp, public_id
from hearthkeep.auth.webauthn import ChallengeStore, Verifier
from hearthkeep.config import LOOPBACK_HOSTS, Settings
from hearthkeep.files.store import ChatFileStore
from hearthkeep.folders import FolderStore
from hearthkeep.images import ImageStore
from hearthkeep.models.cards import CardError, ModelCard
from hearthkeep.numbercheck import NumberChecker
from hearthkeep.runtime.gpu import GpuScheduler
from hearthkeep.sandbox.handle import SandboxHandle
from hearthkeep.sandbox.inspect import Inspector
from hearthkeep.sandbox.runs import RunStore
from hearthkeep.tools.base import ToolRegistry

SESSION_COOKIE = "hk_session"
CSRF_HEADER = "x-hearthkeep"
ENROLLMENT_CODE_TTL_MINUTES = 15
HEALTH_CACHE_SECONDS = 5.0


class ApiError(Exception):
    """Turned into a JSON response `{"reason": reason, ...extra}` with the given status."""

    def __init__(self, status: int, reason: str, **extra: object) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.extra = extra


def error_response(status: int, reason: str, **extra: object) -> JSONResponse:
    return JSONResponse({"reason": reason, **extra}, status_code=status)


class RateLimiter:
    """Sliding one-minute window per (group, client) plus a global cap per group.

    The client is the TCP peer address (uvicorn runs with proxy_headers=False, so
    X-Forwarded-For is ignored). Behind `tailscale serve` every request arrives from 127.0.0.1,
    so there the global cap is what actually bounds guessing. Expired and empty windows are
    pruned on every access and rejected attempts are never stored, so the map cannot grow
    without bound.
    """

    def __init__(
        self,
        per_client: int = 10,
        global_limit: int = 30,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_client = per_client
        self._global = global_limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[tuple[str, str | None], deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        for key in list(self._hits):
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if not hits:
                del self._hits[key]

    def hit(self, group: str, client: str) -> bool:
        """Record an attempt; False (and not recorded) if it exceeds a limit."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            mine = self._hits.get((group, client), ())
            everyone = self._hits.get((group, None), ())
            if len(mine) >= self._per_client or len(everyone) >= self._global:
                return False
            self._hits.setdefault((group, client), deque()).append(now)
            self._hits.setdefault((group, None), deque()).append(now)
            return True

    def size(self) -> int:
        """Number of tracked windows (for tests and diagnostics)."""
        with self._lock:
            return len(self._hits)


HealthCheck = Callable[[], Awaitable[bool]]


@dataclass
class Services:
    settings: Settings
    conn: sqlite3.Connection
    verifier: Verifier
    registry: ToolRegistry
    clock: Callable[[], datetime]
    challenges: ChallengeStore
    sessions: SessionStore
    step_up: StepUp
    codes: EnrollmentCodes
    credentials: CredentialStore
    approvals: ApprovalStore
    runner: TurnRunner
    limiter: RateLimiter
    health_check: HealthCheck
    sse_keepalive: float
    gpu: GpuScheduler
    images: ImageStore
    files: ChatFileStore
    cards: dict[str, ModelCard] = field(default_factory=dict)
    card_errors: list[CardError] = field(default_factory=list)
    managed: frozenset[str] = frozenset()  # card ids the GPU scheduler runs
    chat_model_id: str | None = None  # set when Hearthkeep runs the chat model itself
    monotonic: Callable[[], float] = time.monotonic
    sandbox: SandboxHandle | None = None  # the runner, now or once Docker is seen up
    runs: RunStore | None = None
    inspector: Inspector | None = None
    checker: NumberChecker | None = None
    folders: FolderStore | None = None  # the owner's allowed folders
    health_cache: tuple[float, bool] | None = None
    health_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def chat_model_reachable(self) -> bool:
        """Cached for HEALTH_CACHE_SECONDS so /api/health cannot be used to make this server
        fire a stream of outbound requests."""
        async with self.health_lock:
            now = self.monotonic()
            if self.health_cache is not None and now - self.health_cache[0] < HEALTH_CACHE_SECONDS:
                return self.health_cache[1]
            try:
                reachable = bool(await self.health_check())
            except Exception:
                reachable = False
            self.health_cache = (self.monotonic(), reachable)
            return reachable

    async def chat_model_status(self) -> dict:
        """`chat_model_reachable` and `chat_model_state` for /api/health. A chat model
        Hearthkeep runs itself is never "offline" just because it hasn't started yet: it is
        "ready" once loaded and "starts_on_first_use" before. An external chat server keeps the
        Phase 1 probe: "ready" or "offline"."""
        if self.chat_model_id is not None:
            loaded = self.chat_model_id in self.gpu.resident()
            return {
                "chat_model_reachable": True,
                "chat_model_state": "ready" if loaded else "starts_on_first_use",
            }
        reachable = await self.chat_model_reachable()
        return {
            "chat_model_reachable": reachable,
            "chat_model_state": "ready" if reachable else "offline",
        }

    async def docker_available(self) -> bool:
        """Whether Docker is running now (re-checked at most every 30 s)."""
        if self.sandbox is None:
            return bool(self.settings.docker_available)
        return await self.sandbox.docker_available()

    @property
    def cookie_secure(self) -> bool:
        return urlparse(self.settings.public_origin).hostname not in LOOPBACK_HOSTS


def services(request: Request) -> Services:
    return request.app.state.hk


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, group: str) -> None:
    if not services(request).limiter.hit(group, client_ip(request)):
        raise ApiError(429, "rate_limited")


async def current_session(request: Request) -> Session:
    """FastAPI dependency: the caller's live session, or 401."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise ApiError(401, "unauthenticated")
    session = services(request).sessions.validate(raw)
    if session is None:
        raise ApiError(401, "unauthenticated")
    return session


def actor(session: Session) -> str:
    return f"session:{public_id(session.id)}"


def set_session_cookie(response: JSONResponse, hk: Services, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=hk.settings.session_idle_days * 86400,
        path="/",
        httponly=True,
        samesite="strict",
        secure=hk.cookie_secure,
    )


def clear_session_cookie(response: JSONResponse, hk: Services) -> None:
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, samesite="strict", secure=hk.cookie_secure
    )


def record(hk: Services, who: str, event: str, detail: dict) -> None:
    audit.record(hk.conn, who, event, detail)
