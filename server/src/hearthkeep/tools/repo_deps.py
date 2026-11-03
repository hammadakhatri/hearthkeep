"""`repo_install`: install a repo's dependencies once per lock file, in the repo sandbox.

Outward while the dependencies for the current lock file are not cached (the install has the
internet), Look once they are (running it again does nothing). An install runs only with an
approval for exactly the lock hash it would install: a lock file that changed after the card
was shown, or after the call was classified, is refused. Without Docker or the repo image
nothing runs on the host.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from secrets import token_hex

from hearthkeep.github.deps import (
    ALREADY_INSTALLED,
    DepsCache,
    DepsPlan,
    DepsTooLarge,
    remove_job,
)
from hearthkeep.github.repos import RepoStore
from hearthkeep.permissions import ActionClass
from hearthkeep.tools.base import ToolContext, ToolResult
from hearthkeep.tools.repo import GITHUB_LEVEL_KEY, UNKNOWN_REPO, github_ready

NETWORK_NOTE = "This install has internet access. Only the dependency files are given to it."
NOTHING_TO_INSTALL = (
    "This repo has no uv.lock (with pyproject.toml), requirements.txt or package-lock.json "
    "(with package.json) at its top level, so there is nothing to install."
)
CHANGED_AFTER_APPROVAL = "The dependency files changed after you approved. Nothing was installed."
NEEDS_APPROVAL = "Installing needs the owner's approval for this lock file. Nothing was installed."
NO_LONGER_INSTALLED = (
    "The dependencies are no longer installed. "
    "Call repo_install again so the owner can approve the install."
)
MISSING_CLONE = "The local copy of that repo is missing. Remove it and add it again."


def _fail(message: str) -> ToolResult:
    return ToolResult(ok=False, content=message)


class RepoInstallTool:
    name = "repo_install"
    level_key = GITHUB_LEVEL_KEY
    description = (
        "Install one of the owner's GitHub repos' dependencies (uv.lock, requirements.txt or "
        "package-lock.json) in Hearthkeep's repo sandbox, once per lock file, so repo_test can "
        "run its tests. The install has internet access but sees only the dependency files."
    )
    parameters = {
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "minLength": 3,
                "maxLength": 140,
                "description": "The repo as owner/name.",
            },
        },
        "required": ["repo"],
        "additionalProperties": False,
    }

    def __init__(self, repos: RepoStore, deps: DepsCache, data_dir: Path) -> None:
        self._repos = repos
        self._deps = deps
        self._jobs = Path(data_dir) / "runtime" / "deps"

    def is_available(self) -> bool:
        return github_ready(self._repos)

    def classify(self, args: dict) -> ActionClass:
        """Look when there is nothing to do (an unknown repo, no dependency files, or the
        current lock file already installed); otherwise Outward."""
        repo = self._repos.by_name(args.get("repo", ""))
        if repo is None:
            return ActionClass.LOOK  # `run` refuses without doing anything
        try:
            plan = self._deps.plan(repo, self._repos.dir(repo))
        except (DepsTooLarge, ValueError, OSError):
            return ActionClass.OUTWARD
        if plan is None or self._deps.installed(repo, plan):
            return ActionClass.LOOK
        return ActionClass.OUTWARD

    def summarize(self, args: dict) -> str:
        return f"Install the dependencies of {args.get('repo', 'a repo')}"

    async def approval_detail(self, args: dict, ctx: ToolContext) -> dict | None:
        repo = self._repos.by_name(args["repo"])
        if repo is None:
            return None
        try:
            plan = await asyncio.to_thread(self._deps.plan, repo, self._repos.dir(repo))
        except (DepsTooLarge, ValueError, OSError):
            return None  # `run` gives the model the reason
        if plan is None:
            return None
        return {
            "repo": repo.full_name,
            "command": plan.command,
            "files": list(plan.files),
            "lock_hash": plan.lock_hash,
            "network": NETWORK_NOTE,
        }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        repo = self._repos.by_name(args["repo"])
        if repo is None:
            return _fail(UNKNOWN_REPO)
        clone = self._repos.dir(repo)
        if not clone.is_dir():
            return _fail(MISSING_CLONE)
        try:
            plan = await asyncio.to_thread(self._deps.plan, repo, clone)
        except DepsTooLarge as exc:
            return _fail(str(exc))
        if plan is None:
            return ToolResult(ok=True, content=NOTHING_TO_INSTALL)
        if await self._deps.is_installed(repo, plan):
            return ToolResult(ok=True, content=ALREADY_INSTALLED)
        # A network install runs only on an approved step-up card (Outward). A run that was
        # classified Look (the deps were there) and then found them gone runs nothing.
        if not ctx.approved_step_up:
            return _fail(NO_LONGER_INSTALLED)
        approved = ctx.approved_detail or {}
        if not approved.get("lock_hash"):
            return _fail(NEEDS_APPROVAL)
        if approved.get("lock_hash") != plan.lock_hash:
            return _fail(CHANGED_AFTER_APPROVAL)
        missing = await self._deps.unavailable()
        if missing is not None:
            return _fail(missing)
        return await self._install(repo, plan, clone)

    async def _install(self, repo, plan: DepsPlan, clone: Path) -> ToolResult:
        job = self._jobs / token_hex(8)
        try:
            try:
                await asyncio.to_thread(self._deps.stage, plan, clone, job)
            except ValueError:
                return _fail(CHANGED_AFTER_APPROVAL)
            ok, output = await self._deps.install(repo, plan, job)
        finally:
            await asyncio.to_thread(remove_job, job)
        if ok and output == ALREADY_INSTALLED:  # another call installed it meanwhile
            return ToolResult(ok=True, content=ALREADY_INSTALLED)
        if ok:
            return ToolResult(
                ok=True,
                content=f"Installed the {plan.ecosystem} dependencies.\n{output}".rstrip("\n"),
            )
        return ToolResult(ok=False, content=f"The install failed.\n{output}".rstrip("\n"))
