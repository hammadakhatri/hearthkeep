"""A small GitHub REST client for one fine-grained personal access token.

Talks to https://api.github.com only, never through an environment proxy, never following a
redirect. Paths are limited to `/user` and `/repos/<owner>/<name>/...`. Errors carry fixed
owner-facing copy: GitHub's own message text is never passed on, so the token cannot leak
through an error.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx

TOKEN_SECRET = "github_token"
TOKEN_RE = re.compile(r"github_pat_[A-Za-z0-9_]{20,250}")
REPO_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
TIMEOUT_SECONDS = 20
METHODS = frozenset({"GET", "POST", "PATCH", "PUT"})
DIFF_MEDIA_TYPE = "application/vnd.github.diff"

Reason = Literal["unauthorized", "not_found", "forbidden", "rate_limited", "unavailable", "refused"]

_MESSAGES: dict[str, str] = {
    "unauthorized": "GitHub didn't accept the token. Check it hasn't expired, then save it again.",
    "not_found": "GitHub can't find that repo, or the token can't see it.",
    "forbidden": "GitHub won't let this token do that.",
    "rate_limited": "GitHub says to slow down. Try again in a few minutes.",
    "unavailable": "Can't reach GitHub right now.",
    "refused": "Hearthkeep only talks to GitHub about you and the repos you added.",
}
_PATH_RE = re.compile(r"/repos/(?P<name>[^/]+/[^/]+)(?P<rest>(?:/[A-Za-z0-9._-]+)*)")


class GitHubError(Exception):
    def __init__(self, reason: Reason, status: int | None = None) -> None:
        self.reason: Reason = reason
        self.status = status
        self.message = _MESSAGES[reason]
        super().__init__(self.message)


@dataclass(frozen=True)
class GitHubUser:
    login: str
    id: int


@dataclass(frozen=True)
class RepoInfo:
    full_name: str
    default_branch: str
    private: bool
    can_push: bool


def valid_repo_name(full_name: str) -> bool:
    """`owner/name` by GitHub's rules; the name part is not `.` or `..`."""
    if not isinstance(full_name, str) or not REPO_NAME_RE.fullmatch(full_name):
        return False
    return full_name.split("/", 1)[1] not in (".", "..")


def noreply_email(user: GitHubUser) -> str:
    return f"{user.id}+{user.login}@users.noreply.github.com"


def _path_allowed(path: str) -> bool:
    if path == "/user":
        return True
    match = _PATH_RE.fullmatch(path)
    if match is None or not valid_repo_name(match["name"]):
        return False
    return not any(part in (".", "..") for part in match["rest"].split("/"))


def _error_for(response: httpx.Response) -> GitHubError:
    status = response.status_code
    if status == 401:
        return GitHubError("unauthorized", status)
    if status == 404:
        return GitHubError("not_found", status)
    if status == 429 or (status == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
        return GitHubError("rate_limited", status)
    if status == 403:
        return GitHubError("forbidden", status)
    return GitHubError("unavailable", status)


class GitHubClient:
    def __init__(
        self,
        token: Callable[[], str | None],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._transport = transport

    async def _send(
        self,
        token: str | None,
        method: str,
        path: str,
        params: dict | None = None,
        json: dict | None = None,
        accept: str | None = None,
    ) -> Any:
        if token is None:
            raise GitHubError("unauthorized")
        headers = {
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "Hearthkeep",
            "Authorization": f"Bearer {token}",
        }
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=TIMEOUT_SECONDS,
                transport=self._transport,
            ) as http:
                response = await http.request(
                    method, API_BASE + path, headers=headers, params=params, json=json
                )
        except httpx.HTTPError:
            # The exception text can quote the request; none of it is kept.
            raise GitHubError("unavailable") from None
        if response.status_code >= 300:
            raise _error_for(response)
        if accept is not None:
            return response.text  # a diff: plain text, never parsed
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise GitHubError("unavailable", response.status_code) from None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        accept: str | None = None,
    ) -> Any:
        """One call to GitHub. Only GET, POST, PATCH and PUT (nothing here ever deletes).
        `accept` may ask for a diff (`application/vnd.github.diff`), returned as text."""
        if method not in METHODS or not _path_allowed(path):
            raise GitHubError("refused")
        if accept is not None and accept != DIFF_MEDIA_TYPE:
            raise GitHubError("refused")
        return await self._send(self._token(), method, path, params, json, accept)

    async def user(self, token: str | None = None) -> GitHubUser:
        """The account the token belongs to; `token` checks one that is not saved yet."""
        data = await self._send(token if token is not None else self._token(), "GET", "/user")
        try:
            return GitHubUser(login=str(data["login"]), id=int(data["id"]))
        except (KeyError, TypeError, ValueError):
            raise GitHubError("unavailable") from None

    async def repo(self, full_name: str) -> RepoInfo:
        if not valid_repo_name(full_name):
            raise GitHubError("refused")
        data = await self.request("GET", f"/repos/{full_name}")
        try:
            return RepoInfo(
                full_name=str(data["full_name"]),
                default_branch=str(data["default_branch"]),
                private=bool(data["private"]),
                can_push=bool((data.get("permissions") or {}).get("push", False)),
            )
        except (KeyError, TypeError, AttributeError):
            raise GitHubError("unavailable") from None
