"""Shared pieces of the HTTP API: the service container, errors, session and rate limiting."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
from hearthkeep.tools.base import ToolRegistry

SESSION_COOKIE = "hk_session"
CSRF_HEADER = "x-hearthkeep"
ENROLLMENT_CODE_TTL_MINUTES = 15


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

    Behind `tailscale serve` every request may come from 127.0.0.1, so the global cap is what
    actually bounds guessing in that setup.
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

    def _recent(self, key: tuple[str, str | None], now: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        return hits

    def hit(self, group: str, client: str) -> bool:
        """Record an attempt; False (and not recorded) if it exceeds a limit."""
        now = self._clock()
        with self._lock:
            mine = self._recent((group, client), now)
            everyone = self._recent((group, None), now)
            if len(mine) >= self._per_client or len(everyone) >= self._global:
                return False
            mine.append(now)
            everyone.append(now)
            return True


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

    @property
    def docker_available(self) -> bool:
        return bool(self.settings.docker_available)

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
