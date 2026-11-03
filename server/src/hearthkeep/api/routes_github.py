"""The GitHub token: save or replace it (passkey step-up; it widens what Hearthkeep can reach)
and remove it (session only; it narrows). The token is checked with GitHub before it is saved
and never appears in a response or the audit log."""

from __future__ import annotations

import shutil
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from hearthkeep.api.common import ApiError, Services, actor, current_session, record, services
from hearthkeep.auth.sessions import Session
from hearthkeep.db import transaction, utcnow
from hearthkeep.github.api import TOKEN_RE, TOKEN_SECRET, GitHubClient, GitHubError
from hearthkeep.secrets import SecretStoreError

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]

SECRETS_UNREADABLE = "Hearthkeep couldn't read its stored secrets. Check the server."


class TokenBody(BaseModel):
    token: str = Field(max_length=512)
    step_up_token: str = Field(max_length=256)


def _github(hk: Services) -> GitHubClient:
    if hk.github is None or hk.secrets is None:
        raise ApiError(503, "unavailable")
    return hk.github


@router.get("/github")
async def github_state(session: SessionDep, request: Request) -> dict:
    hk = services(request)
    row = hk.conn.execute("SELECT login FROM github_account WHERE id = 1").fetchone()
    return {
        "git": shutil.which("git") is not None,
        "connected": row is not None,
        "login": row["login"] if row else None,
    }


@router.put("/github/token")
async def save_token(body: TokenBody, session: SessionDep, request: Request) -> dict:
    hk = services(request)
    github = _github(hk)
    if not hk.step_up.check(body.step_up_token, session.id, None, "github_token"):
        record(hk, actor(session), "github.token_denied", {"reason": "step_up_required"})
        raise ApiError(403, "step_up_required")
    if not TOKEN_RE.fullmatch(body.token):
        raise ApiError(422, "token_format")
    try:
        user = await github.user(body.token)
    except GitHubError as error:
        if error.reason in ("unauthorized", "forbidden"):
            raise ApiError(422, "token_rejected", message=error.message) from None
        raise ApiError(502, "github_unavailable", message=error.message) from None
    try:
        hk.secrets.put(TOKEN_SECRET, body.token)
    except SecretStoreError:
        raise ApiError(503, "secrets_unreadable", message=SECRETS_UNREADABLE) from None
    with transaction(hk.conn):
        hk.conn.execute(
            "INSERT INTO github_account (id, login, user_id, checked_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET login = excluded.login, "
            "user_id = excluded.user_id, checked_at = excluded.checked_at",
            (user.login, user.id, utcnow()),
        )
    record(hk, actor(session), "github.token_saved", {})
    return {"connected": True, "login": user.login}


@router.delete("/github/token")
async def remove_token(session: SessionDep, request: Request) -> dict:
    hk = services(request)
    _github(hk)
    try:
        hk.secrets.delete(TOKEN_SECRET)
    except SecretStoreError:
        raise ApiError(503, "secrets_unreadable", message=SECRETS_UNREADABLE) from None
    with transaction(hk.conn):
        hk.conn.execute("DELETE FROM github_account")
    record(hk, actor(session), "github.token_removed", {})
    return {"connected": False, "login": None}
