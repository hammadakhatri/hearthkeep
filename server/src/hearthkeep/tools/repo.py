"""Tools for the owner's GitHub repos: `repo_read` looks at a clone, never changes it.

Nothing the model writes is read as a git option: the search text follows `-e`, paths follow
`--` (as literal pathspecs), and a `ref` must be a plain name that resolves to a commit, which
is then used by its full hash.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from hearthkeep.github.diff import cap_diff
from hearthkeep.github.git import GitError
from hearthkeep.github.paths import PathRefused, safe_path
from hearthkeep.github.repos import Repo, RepoStore
from hearthkeep.permissions import ActionClass
from hearthkeep.tools.base import ToolContext, ToolResult

GITHUB_LEVEL_KEY = "github"
UNKNOWN_REPO = "That repo isn't added to Hearthkeep. Add it in Repos first."

LIST_MAX_ENTRIES = 500
READ_MAX_LINES = 400
READ_MAX_BYTES = 1024 * 1024
READ_LINE_CHARS = 2000
BINARY_SNIFF_BYTES = 8192
SEARCH_MAX_MATCHES = 200
SEARCH_LINE_CHARS = 300
STATUS_MAX_FILES = 200
LOG_COMMITS = 20
LOG_LINE_CHARS = 300
QUERY_MAX_CHARS = 200
REF_MAX_CHARS = 200
GIT_TIMEOUT_S = 60.0
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: junctions and other Windows links

_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/@^~{}-]*")
_SHA = re.compile(r"[0-9a-f]{40,64}")
_ACTIONS = ["list", "read", "search", "status", "diff", "log", "fetch"]


def github_ready(repos: RepoStore | None) -> bool:
    """Whether the GitHub tools can work at all: host git is installed and a token is saved.
    Every GitHub tool's `is_available` is this."""
    if repos is None:
        return False
    return repos.git.available() and repos.token() is not None


def _cut(line: str, limit: int) -> str:
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _fail(message: str) -> ToolResult:
    return ToolResult(ok=False, content=message)


class RepoReadTool:
    name = "repo_read"
    level_key = GITHUB_LEVEL_KEY
    description = (
        "Look at one of the owner's GitHub repos from its local copy: list files, read a file, "
        "search the code, see the status, the diff against the default branch or recent "
        "commits, or fetch the latest from GitHub. Never changes anything."
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
            "action": {"type": "string", "enum": _ACTIONS},
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 400,
                "description": "A path inside the repo, like src/app.py (list, read, search).",
            },
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": "First line to read (read); at most 400 lines come back.",
            },
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": QUERY_MAX_CHARS,
                "description": "Text to look for, matched literally (search).",
            },
            "ref": {
                "type": "string",
                "minLength": 1,
                "maxLength": REF_MAX_CHARS,
                "description": "A branch, tag or commit: where log starts, or what diff "
                "compares with (default HEAD and the default branch).",
            },
        },
        "required": ["repo", "action"],
        "additionalProperties": False,
    }

    def __init__(self, repos: RepoStore) -> None:
        self._repos = repos

    def is_available(self) -> bool:
        return github_ready(self._repos)

    def classify(self, args: dict) -> ActionClass:
        return ActionClass.LOOK

    def summarize(self, args: dict) -> str:
        what = {
            "list": "List files in",
            "read": "Read a file in",
            "search": "Search",
            "status": "Check the status of",
            "diff": "Show the changes in",
            "log": "Show recent commits of",
            "fetch": "Fetch the latest for",
        }.get(args.get("action"), "Look at")
        return f"{what} {args.get('repo', 'a repo')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        repo = self._repos.by_name(args["repo"])
        if repo is None:
            return _fail(UNKNOWN_REPO)
        clone = self._repos.dir(repo)
        if not clone.is_dir():
            return _fail("The local copy of that repo is missing. Remove it and add it again.")
        action = args["action"]
        try:
            if action == "list":
                return await asyncio.to_thread(self._list, clone, args.get("path"))
            if action == "read":
                return await asyncio.to_thread(
                    self._read, clone, args.get("path"), args.get("start_line", 1)
                )
            if action == "search":
                return await self._search(clone, args.get("query"), args.get("path"))
            if action == "status":
                return await self._status(repo)
            if action == "diff":
                return await self._diff(repo, clone, args.get("ref"))
            if action == "log":
                return await self._log(clone, args.get("ref"))
            if action == "fetch":
                await self._repos.fetch(repo)
                return ToolResult(ok=True, content="Fetched the latest from GitHub.")
        except PathRefused as refused:
            return _fail(refused.message)
        except GitError as error:
            return _fail(error.message)
        return _fail(f"Unknown action: {action}.")

    # -- files -----------------------------------------------------------------------------

    def _list(self, clone: Path, rel: str | None) -> ToolResult:
        base = safe_path(clone, rel) if rel else clone
        if not base.is_dir():
            return _fail("That path isn't a folder in the repo.")
        prefix = "" if not rel else rel.rstrip("/") + "/"
        entries: list[str] = []
        truncated = False

        def walk(folder: Path, shown: str, depth: int) -> None:
            nonlocal truncated
            try:
                with os.scandir(folder) as found:
                    children = sorted(found, key=lambda e: e.name)
            except OSError:
                return
            for child in children:
                if child.name == ".git" and folder == clone:
                    continue
                if len(entries) >= LIST_MAX_ENTRIES:
                    truncated = True
                    return
                try:
                    st = child.stat(follow_symlinks=False)
                    link = child.is_symlink() or bool(
                        getattr(st, "st_file_attributes", 0) & _REPARSE_POINT
                    )
                    is_dir = child.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if link:
                    entries.append(f"{shown}{child.name}@")  # a link: named, never followed
                    continue
                entries.append(f"{shown}{child.name}{'/' if is_dir else ''}")
                if is_dir and depth < 2:
                    walk(Path(child.path), f"{shown}{child.name}/", depth + 1)
                    if truncated:
                        return

        walk(base, prefix, 1)
        if not entries:
            return ToolResult(ok=True, content="That folder is empty.")
        text = "\n".join(entries)
        if truncated:
            text += f"\n… only the first {LIST_MAX_ENTRIES} entries are shown."
        return ToolResult(ok=True, content=text)

    def _read(self, clone: Path, rel: str | None, start_line: int) -> ToolResult:
        if not rel:
            return _fail("Reading a file needs 'path'.")
        target = safe_path(clone, rel)
        if not target.is_file():
            return _fail("That path isn't a file in the repo.")
        if target.stat().st_size > READ_MAX_BYTES:
            return _fail("That file is larger than 1 MB, so it isn't read.")
        with open(target, "rb") as handle:
            data = handle.read(READ_MAX_BYTES + 1)
        if b"\0" in data[:BINARY_SNIFF_BYTES]:
            return _fail("That looks like a binary file, so it isn't read.")
        lines = data.decode("utf-8", "replace").splitlines()
        if not lines:
            return ToolResult(ok=True, content="The file is empty.")
        first = start_line - 1
        if first >= len(lines):
            return _fail(f"The file has only {len(lines)} lines.")
        window = lines[first : first + READ_MAX_LINES]
        text = "\n".join(
            f"{first + i + 1}\t{_cut(line, READ_LINE_CHARS)}" for i, line in enumerate(window)
        )
        last = first + len(window)
        if last < len(lines):
            text += f"\n… {len(lines) - last} more lines; read on with start_line={last + 1}."
        return ToolResult(ok=True, content=text)

    # -- git -------------------------------------------------------------------------------

    async def _search(self, clone: Path, query: str | None, rel: str | None) -> ToolResult:
        if not query:
            return _fail("Searching needs 'query'.")
        argv = ["grep", "-n", "-I", "-F", "--no-color", "-e", query]
        if rel:
            safe_path(clone, rel)
            argv += ["--", f":(literal){rel}"]
        result = await self._repos.git.run(clone, *argv, timeout=GIT_TIMEOUT_S, check=False)
        if result.code == 1 and not result.out.strip():
            return ToolResult(ok=True, content="No matches.")
        if result.code != 0:
            detail = result.err.strip()[-300:] or "no error output"
            return _fail(f"The search failed: {detail}")
        rows = [r for r in result.out.splitlines() if r]
        text = "\n".join(_cut(row, SEARCH_LINE_CHARS) for row in rows[:SEARCH_MAX_MATCHES])
        if len(rows) > SEARCH_MAX_MATCHES:
            text += f"\n… and {len(rows) - SEARCH_MAX_MATCHES} more matches; narrow the search."
        return ToolResult(ok=True, content=text or "No matches.")

    async def _status(self, repo: Repo) -> ToolResult:
        status = await self._repos.status(repo)
        lines = [
            f"Branch: {status.branch}",
            f"Ahead of GitHub by {status.ahead}, behind by {status.behind}.",
        ]
        if status.changed:
            lines.append(f"Changed files ({len(status.changed)}):")
            lines += [f"  {name}" for name in status.changed[:STATUS_MAX_FILES]]
            if len(status.changed) > STATUS_MAX_FILES:
                lines.append(f"  … and {len(status.changed) - STATUS_MAX_FILES} more")
        else:
            lines.append("No changed files.")
        return ToolResult(ok=True, content="\n".join(lines))

    async def _resolve(self, clone: Path, ref: str) -> str:
        """The full commit hash of `ref`. A ref is never passed to git as typed: it must look
        like a plain name (so it cannot be an option) and resolve to a commit."""
        if len(ref) > REF_MAX_CHARS or not _REF.fullmatch(ref):
            raise GitError("That isn't a branch, tag or commit name I can use.")
        result = await self._repos.git.run(
            clone, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False
        )
        sha = result.out.strip()
        if result.code != 0 or not _SHA.fullmatch(sha):
            raise GitError("That branch, tag or commit isn't in the repo.")
        return sha

    async def _diff(self, repo: Repo, clone: Path, ref: str | None) -> ToolResult:
        git = self._repos.git
        if ref:
            base = await self._resolve(clone, ref)
        else:
            remote = f"refs/remotes/origin/{repo.default_branch}"
            default = await git.run(
                clone, "rev-parse", "--verify", "--quiet", f"{remote}^{{commit}}", check=False
            )
            if default.code != 0:
                return _fail(
                    f"The repo has no origin/{repo.default_branch} yet. Fetch it first with "
                    'action "fetch".'
                )
            merge = await git.run(clone, "merge-base", "HEAD", default.out.strip(), check=False)
            if merge.code != 0:
                return _fail("This branch shares no history with the default branch.")
            base = merge.out.strip()
        diff = await git.run(
            clone, "diff", "--no-color", "--no-ext-diff", "--no-textconv", base, "--",
            timeout=GIT_TIMEOUT_S,
        )  # fmt: skip
        untracked = await git.run(
            clone, "ls-files", "--others", "--exclude-standard", "-z", check=False
        )
        new_files = [n for n in untracked.out.split("\0") if n]
        parts = [diff.out] if diff.out.strip() else []
        if new_files:
            names = "\n".join(f"  {n}" for n in new_files[:STATUS_MAX_FILES])
            parts.append(f"Untracked files ({len(new_files)}):\n{names}")
        if not parts:
            return ToolResult(ok=True, content="No changes.")
        return ToolResult(ok=True, content=cap_diff("\n".join(parts)))

    async def _log(self, clone: Path, ref: str | None) -> ToolResult:
        start = await self._resolve(clone, ref or "HEAD")
        result = await self._repos.git.run(
            clone, "log", "-n", str(LOG_COMMITS), "--date=short", "--format=%h %ad %s",
            start, "--",
        )  # fmt: skip
        rows = [_cut(r, LOG_LINE_CHARS) for r in result.out.splitlines() if r]
        return ToolResult(ok=True, content="\n".join(rows) or "No commits yet.")
