"""The GitHub token: save or replace it (passkey step-up; it widens what Hearthkeep can reach)
and remove it (session only; it narrows). The token is checked with GitHub before it is saved
and never appears in a response or the audit log.

The repos: adding one is a passkey step-up (`add_repo`); removing one deletes its clone, so it
is a `delete` step-up bound to exactly that repo. The audit log records repo ids, never names.
Each repo's test runs are listed (without their output) and read one at a time."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from hearthkeep.api.common import ApiError, Services, actor, current_session, record, services
from hearthkeep.auth.sessions import Session
from hearthkeep.db import transaction, utcnow
from hearthkeep.github.api import TOKEN_RE, TOKEN_SECRET, GitHubClient, GitHubError
from hearthkeep.github.deps import DepsCache
from hearthkeep.github.git import GitError
from hearthkeep.github.repos import Repo, RepoError, RepoStore
from hearthkeep.github.testrun import RepoRun, RepoTestRunner
from hearthkeep.secrets import SecretStoreError

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]

SECRETS_UNREADABLE = "Hearthkeep couldn't read its stored secrets. Check the server."
REPO_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
RUNS_LISTED = 20
_REPO_ERROR_STATUS = {
    "no_token": 409,
    "name_format": 422,
    "repo_not_found": 404,
    "already_added": 409,
    "clone_failed": 502,
}


class TokenBody(BaseModel):
    token: str = Field(max_length=512)
    step_up_token: str = Field(max_length=256)


class AddRepoBody(BaseModel):
    full_name: str = Field(max_length=200)
    step_up_token: str = Field(max_length=256)


class RemoveRepoBody(BaseModel):
    step_up_token: str = Field(max_length=256)


def _github(hk: Services) -> GitHubClient:
    if hk.github is None or hk.secrets is None:
        raise ApiError(503, "unavailable")
    return hk.github


def _repos(hk: Services) -> RepoStore:
    if hk.repos is None:
        raise ApiError(503, "unavailable")
    return hk.repos


def _repo(repos: RepoStore, repo_id: str) -> Repo:
    repo = repos.get(repo_id) if REPO_ID.fullmatch(repo_id) else None
    if repo is None:
        raise ApiError(404, "not_found")
    return repo


def _run_json(run: RepoRun, with_output: bool = False) -> dict:
    body = {
        "id": run.id,
        "chat_id": run.chat_id,
        "command": run.command,
        "status": run.status,
        "exit_code": run.exit_code,
        "duration_ms": run.duration_ms,
        "created_at": run.created_at,
    }
    if with_output:
        body["repo_id"] = run.repo_id
        body["output"] = run.output
    return body


def _last_run(tests: RepoTestRunner | None, repo: Repo) -> dict | None:
    runs = tests.runs(repo.id, limit=1) if tests is not None else []
    return _run_json(runs[0]) if runs else None


async def _repo_json(
    repos: RepoStore, repo: Repo, deps: DepsCache | None, tests: RepoTestRunner | None = None
) -> dict:
    try:
        status = await repos.status(repo)
    except GitError:
        status = None  # the clone is missing or broken: shown without a branch
    deps_state = await deps.current_state(repo, repos.dir(repo)) if deps is not None else None
    return {
        "id": repo.id,
        "full_name": repo.full_name,
        "default_branch": repo.default_branch,
        "private": repo.private,
        "branch": status.branch if status else None,
        "changed": status.changed if status else [],
        "ahead": status.ahead if status else 0,
        "behind": status.behind if status else 0,
        "deps": deps_state,
        "last_run": _last_run(tests, repo),
    }


@router.get("/github")
async def github_state(session: SessionDep, request: Request) -> dict:
    hk = services(request)
    row = hk.conn.execute("SELECT login FROM github_account WHERE id = 1").fetchone()
    return {
        "git": hk.repos is not None and await run_in_threadpool(hk.repos.git.available),
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


@router.get("/repos")
async def list_repos(session: SessionDep, request: Request) -> dict:
    hk = services(request)
    repos = _repos(hk)
    return {
        "repos": [await _repo_json(repos, repo, hk.deps, hk.repo_tests) for repo in repos.list()]
    }


@router.post("/repos")
async def add_repo(body: AddRepoBody, session: SessionDep, request: Request) -> JSONResponse:
    hk = services(request)
    repos = _repos(hk)
    if not hk.step_up.check(body.step_up_token, session.id, None, "add_repo"):
        record(hk, actor(session), "repos.add_denied", {"reason": "step_up_required"})
        raise ApiError(403, "step_up_required")
    try:
        repo = await repos.add(body.full_name)
    except RepoError as error:
        raise ApiError(
            _REPO_ERROR_STATUS[error.reason], error.reason, message=error.message
        ) from None
    except GitHubError as error:
        raise ApiError(502, "github_unavailable", message=error.message) from None
    record(hk, actor(session), "repos.added", {"id": repo.id})
    return JSONResponse(await _repo_json(repos, repo, hk.deps, hk.repo_tests), status_code=201)


@router.delete("/repos/{repo_id}")
async def remove_repo(
    repo_id: str, body: RemoveRepoBody, session: SessionDep, request: Request
) -> dict:
    """Remove the repo and its clone, with a step-up for exactly `repo:<id>`."""
    hk = services(request)
    repos = _repos(hk)
    repo = _repo(repos, repo_id)
    if not hk.step_up.check(body.step_up_token, session.id, f"repo:{repo.id}", "delete"):
        record(hk, actor(session), "repos.remove_denied",
               {"id": repo.id, "reason": "step_up_required"})  # fmt: skip
        raise ApiError(403, "step_up_required")
    await repos.remove(repo.id)
    record(hk, actor(session), "repos.removed", {"id": repo.id})
    return {"ok": True}


@router.post("/repos/{repo_id}/fetch")
async def fetch_repo(repo_id: str, session: SessionDep, request: Request) -> dict:
    hk = services(request)
    repos = _repos(hk)
    repo = _repo(repos, repo_id)
    try:
        await repos.fetch(repo)
    except GitError as error:
        raise ApiError(502, "fetch_failed", message=error.message) from None
    record(hk, actor(session), "repos.fetched", {"id": repo.id})
    return await _repo_json(repos, repo, hk.deps, hk.repo_tests)


def _tests(hk: Services) -> RepoTestRunner:
    if hk.repo_tests is None:
        raise ApiError(503, "unavailable")
    return hk.repo_tests


@router.get("/repos/{repo_id}/runs")
async def list_runs(repo_id: str, session: SessionDep, request: Request) -> dict:
    """The repo's last 20 test runs, newest first, without their output."""
    hk = services(request)
    repo = _repo(_repos(hk), repo_id)
    return {"runs": [_run_json(run) for run in _tests(hk).runs(repo.id, limit=RUNS_LISTED)]}


@router.get("/repo-runs/{run_id}")
async def get_run(run_id: str, session: SessionDep, request: Request) -> dict:
    """One test run with its output (the first 64 KB and the last 192 KB)."""
    hk = services(request)
    run = _tests(hk).get(run_id) if RUN_ID.fullmatch(run_id) else None
    if run is None:
        raise ApiError(404, "not_found")
    return _run_json(run, with_output=True)
