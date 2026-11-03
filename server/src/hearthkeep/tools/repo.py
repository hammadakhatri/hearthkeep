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
from secrets import token_hex

from hearthkeep.github.api import GitHubUser, noreply_email
from hearthkeep.github.deps import MODEL_OUTPUT_CHARS, for_model
from hearthkeep.github.diff import cap_diff, file_diff
from hearthkeep.github.git import GitError
from hearthkeep.github.paths import BRANCH_PREFIX, BRANCH_SLUG, PathRefused, safe_path
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
# The model's context is small and every tool message is replayed each turn, so nothing
# repo_read returns is longer than for_model's 8,000 characters; what was cut says how to
# ask for less.
READ_BUDGET_CHARS = MODEL_OUTPUT_CHARS - 200  # room for the "read on" line
_NARROW = {
    "list": "narrow it with 'path'",
    "read": "read on with 'start_line'",
    "search": "narrow it with a longer 'query' or a 'path'",
    "diff": "narrow it with 'path'",
    "log": "narrow it with 'ref'",
    "status": "see one folder's changes with diff and a 'path'",
}


def github_ready(repos: RepoStore | None) -> bool:
    """Whether the GitHub tools can work at all: host git is installed and a token is saved.
    Every GitHub tool's `is_available` is this."""
    if repos is None:
        return False
    return repos.git.available() and repos.token_saved()


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
                "description": "A path inside the repo, like src/app.py (list, read, search, "
                "diff).",
            },
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": "First line to read (read); at most 400 lines or about "
                "8,000 characters come back.",
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
        result = await self._action(repo, clone, action, args)
        narrow = _NARROW.get(action, "ask for less")
        return ToolResult(ok=result.ok, content=for_model(result.content, narrow))

    async def _action(self, repo: Repo, clone: Path, action: str, args: dict) -> ToolResult:
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
                return await self._diff(repo, clone, args.get("ref"), args.get("path"))
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
        shown: list[str] = []
        used = 0
        for number in range(first, min(first + READ_MAX_LINES, len(lines))):
            row = f"{number + 1}\t{_cut(lines[number], READ_LINE_CHARS)}"
            if shown and used + len(row) + 1 > READ_BUDGET_CHARS:
                break  # whole lines only: the next read starts where this one stopped
            shown.append(row)
            used += len(row) + 1
        text = "\n".join(shown)
        last = first + len(shown)
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

    async def _diff(
        self, repo: Repo, clone: Path, ref: str | None, rel: str | None = None
    ) -> ToolResult:
        git = self._repos.git
        scope: list[str] = []
        if rel:
            safe_path(clone, rel)
            scope = [f":(literal){rel}"]
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
            clone, "diff", "--no-color", "--no-ext-diff", "--no-textconv", base, "--", *scope,
            timeout=GIT_TIMEOUT_S,
        )  # fmt: skip
        untracked = await git.run(
            clone, "ls-files", "--others", "--exclude-standard", "-z", "--", *scope, check=False
        )
        new_files = [n for n in untracked.out.split("\0") if n]
        parts = [diff.out] if diff.out.strip() else []
        if new_files:
            names = "\n".join(f"  {n}" for n in new_files[:STATUS_MAX_FILES])
            parts.append(f"Untracked files ({len(new_files)}):\n{names}")
        if not parts:
            return ToolResult(ok=True, content="No changes.")
        return ToolResult(ok=True, content="\n".join(parts))  # cut for the model by `run`

    async def _log(self, clone: Path, ref: str | None) -> ToolResult:
        start = await self._resolve(clone, ref or "HEAD")
        result = await self._repos.git.run(
            clone, "log", "-n", str(LOG_COMMITS), "--date=short", "--format=%h %ad %s",
            start, "--",
        )  # fmt: skip
        rows = [_cut(r, LOG_LINE_CHARS) for r in result.out.splitlines() if r]
        return ToolResult(ok=True, content="\n".join(rows) or "No commits yet.")


# -- repo_edit -----------------------------------------------------------------------------

EDIT_MAX_BYTES = 512 * 1024
COMMIT_MESSAGE_MAX = 2000
COMMIT_SUBJECT_MAX = 72
NEED_BRANCH = 'Create a hearthkeep/ branch first with repo_edit action "branch".'
NO_ACCOUNT = "No GitHub account is saved yet. Save your GitHub token in Settings first."
NOT_TEXT = "That file isn't plain UTF-8 text, so it isn't edited."
_EDIT_ACTIONS = ["branch", "write", "replace", "delete", "commit"]
_STAGED_DIFF = ("diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv")


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")


class RepoEditTool:
    name = "repo_edit"
    level_key = GITHUB_LEVEL_KEY
    description = (
        "Change one of the owner's GitHub repos in its local copy, only on a branch named "
        'hearthkeep/<name>. First make one with action "branch" (it starts from the default '
        'branch as last fetched), then "write" a whole file, "replace" one piece of text in a '
        'file, or "delete" a file, and "commit" the changes. Nothing reaches GitHub.'
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
            "action": {"type": "string", "enum": _EDIT_ACTIONS},
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 40,
                "description": "Branch name without the hearthkeep/ prefix: lowercase letters, "
                "digits and dashes (branch).",
            },
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 400,
                "description": "A path inside the repo (write, replace, delete).",
            },
            "content": {
                "type": "string",
                "maxLength": EDIT_MAX_BYTES,
                "description": "The whole new file, at most 512 KB (write).",
            },
            "old": {
                "type": "string",
                "minLength": 1,
                "maxLength": EDIT_MAX_BYTES,
                "description": "The text to replace; must occur exactly once (replace).",
            },
            "new": {
                "type": "string",
                "maxLength": EDIT_MAX_BYTES,
                "description": "What to put there instead (replace).",
            },
            "message": {
                "type": "string",
                "minLength": 1,
                "maxLength": COMMIT_MESSAGE_MAX,
                "description": "Commit message: first line at most 72 characters (commit).",
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
        return ActionClass.CHANGE_LOCAL

    def summarize(self, args: dict) -> str:
        what = {
            "branch": "Start a branch in",
            "write": "Write a file in",
            "replace": "Change a file in",
            "delete": "Delete a file in",
            "commit": "Commit the changes in",
        }.get(args.get("action"), "Change")
        return f"{what} {args.get('repo', 'a repo')}"

    # -- helpers ---------------------------------------------------------------------------

    async def _branch(self, clone: Path) -> str | None:
        result = await self._repos.git.run(
            clone, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
        )
        name = result.out.strip()
        return name if result.code == 0 and name else None

    @staticmethod
    def _account(conn) -> GitHubUser | None:
        row = conn.execute("SELECT login, user_id FROM github_account WHERE id = 1").fetchone()
        if row is None or not row["login"] or row["user_id"] is None:
            return None
        return GitHubUser(login=row["login"], id=int(row["user_id"]))

    @staticmethod
    def _read_text(target: Path) -> tuple[str, bool]:
        """(text with LF line ends, whether the file used CRLF) of an existing text file."""
        if target.stat().st_size > EDIT_MAX_BYTES:
            raise ValueError(NOT_TEXT)  # refused before a byte of it is read
        raw = target.read_bytes()
        if b"\0" in raw[:BINARY_SNIFF_BYTES]:
            raise ValueError(NOT_TEXT)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(NOT_TEXT) from None
        return _lf(text), b"\r\n" in raw

    def _plan(self, clone: Path, args: dict) -> tuple[str, Path, str | None, str | None, bool]:
        """For write, replace or delete: (rel, target, old text, new text, crlf), texts with
        LF line ends. Raises PathRefused or ValueError (a message for the model)."""
        action = args["action"]
        rel = args.get("path")
        if not rel:
            raise ValueError(f"{action.capitalize()} needs 'path'.")
        target = safe_path(clone, rel)
        if target.is_dir():
            raise ValueError("That path is a folder, not a file.")
        exists = target.is_file()
        old: str | None = None
        crlf = False
        if action == "delete":
            if not exists:
                raise ValueError("That path isn't a file in the repo.")
            try:
                old, _ = self._read_text(target)
            except ValueError:
                old = None  # binary or large: removed all the same, shown without a text diff
            return rel, target, old, None, False
        if exists:
            old, crlf = self._read_text(target)
        if action == "write":
            content = args.get("content")
            if not isinstance(content, str):
                raise ValueError("Write needs 'content'.")
            if len(content.encode("utf-8", "replace")) > EDIT_MAX_BYTES:
                raise ValueError("That content is larger than 512 KB.")
            return rel, target, old, _lf(content), crlf
        if old is None:
            raise ValueError("That path isn't a file in the repo.")
        needle, new = args.get("old"), args.get("new")
        if not needle or not isinstance(new, str):
            raise ValueError("Replace needs 'old' and 'new'.")
        needle, new = _lf(needle), _lf(new)
        found = old.count(needle)
        if found != 1:
            raise ValueError(
                f"The text to replace was found {found} times; it must match exactly once."
            )
        return rel, target, old, old.replace(needle, new, 1), crlf

    async def _preview_commit(self, clone: Path) -> str:
        """What `git add -A` then `git commit` would record, computed in a throwaway index
        (under runtime/, deleted after) so the clone's own index is never touched. Untracked
        files are hashed into the object store as unreachable loose objects, which git's gc
        removes; there is no way to render their diff without that."""
        git = self._repos.git
        index = git.home / f"index-{token_hex(8)}"
        try:
            await git.run(clone, "read-tree", "HEAD", index_file=index, timeout=GIT_TIMEOUT_S)
            await git.run(clone, "add", "-A", index_file=index, timeout=GIT_TIMEOUT_S)
            staged = await git.run(clone, *_STAGED_DIFF, index_file=index, timeout=GIT_TIMEOUT_S)
            return staged.out
        finally:
            index.unlink(missing_ok=True)
            Path(str(index) + ".lock").unlink(missing_ok=True)

    # -- approval --------------------------------------------------------------------------

    async def approval_detail(self, args: dict, ctx: ToolContext) -> dict | None:
        repo = self._repos.by_name(args["repo"])
        if repo is None:
            return None
        clone = self._repos.dir(repo)
        action = args["action"]
        if action == "branch":
            return {"repo": repo.full_name, "branch": BRANCH_PREFIX + str(args.get("name", ""))}
        branch = await self._branch(clone) or ""
        detail: dict = {"repo": repo.full_name, "branch": branch, "diff": ""}
        if action == "commit":
            detail["message"] = str(args.get("message", ""))
            if branch.startswith(BRANCH_PREFIX):
                try:
                    detail["diff"] = cap_diff(await self._preview_commit(clone))
                except GitError:
                    pass  # `run` gives the model the reason
            return detail
        detail["path"] = str(args.get("path", ""))
        try:
            rel, _target, old, new, _crlf = self._plan(clone, args)
            if action == "delete" and old is None:
                detail["diff"] = f"Binary or large file {rel} deleted\n"
            else:
                detail["diff"] = cap_diff(file_diff(rel, old, new))
        except (PathRefused, ValueError, OSError):
            pass  # `run` gives the model the reason
        return detail

    # -- run -------------------------------------------------------------------------------

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        repo = self._repos.by_name(args["repo"])
        if repo is None:
            return _fail(UNKNOWN_REPO)
        clone = self._repos.dir(repo)
        if not clone.is_dir():
            return _fail("The local copy of that repo is missing. Remove it and add it again.")
        action = args["action"]
        try:
            if action == "branch":
                return await self._start_branch(repo, clone, args.get("name"))
            branch = await self._branch(clone)
            if branch is None or not branch.startswith(BRANCH_PREFIX):
                return _fail(NEED_BRANCH)
            if action == "commit":
                return await self._commit(clone, branch, args.get("message"), ctx)
            if action in ("write", "replace", "delete"):
                return await asyncio.to_thread(self._change, clone, args)
        except PathRefused as refused:
            return _fail(refused.message)
        except ValueError as error:
            return _fail(str(error))
        except GitError as error:
            return _fail(error.message)
        except OSError:
            return _fail("The file couldn't be changed.")
        return _fail(f"Unknown action: {action}.")

    async def _start_branch(self, repo: Repo, clone: Path, name: str | None) -> ToolResult:
        if not isinstance(name, str) or not BRANCH_SLUG.fullmatch(name):
            return _fail(
                "A branch name is lowercase letters, digits and dashes, up to 40 characters, "
                "starting with a letter or digit."
            )
        git = self._repos.git
        full = BRANCH_PREFIX + name
        exists = await git.run(
            clone, "rev-parse", "--verify", "--quiet", f"refs/heads/{full}", check=False
        )
        if exists.code == 0:
            await git.run(clone, "switch", "--quiet", full, "--")
            return ToolResult(ok=True, content=f"Switched to the existing branch {full}.")
        start = f"refs/remotes/origin/{repo.default_branch}"
        found = await git.run(
            clone, "rev-parse", "--verify", "--quiet", f"{start}^{{commit}}", check=False
        )
        if found.code != 0:
            return _fail(
                f"The repo has no origin/{repo.default_branch} yet. Fetch it first with "
                'repo_read action "fetch".'
            )
        await git.run(clone, "switch", "--quiet", "--no-track", "-c", full, start, "--")
        return ToolResult(ok=True, content=f"Started {full} from origin/{repo.default_branch}.")

    def _change(self, clone: Path, args: dict) -> ToolResult:
        rel, target, _old, new, crlf = self._plan(clone, args)
        if args["action"] == "delete":
            target.unlink()
            return ToolResult(ok=True, content=f"Deleted {rel}.")
        assert new is not None
        text = new.replace("\n", "\r\n") if crlf else new
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        verb = "Wrote" if args["action"] == "write" else "Changed"
        return ToolResult(ok=True, content=f"{verb} {rel}.")

    async def _commit(
        self, clone: Path, branch: str, message: str | None, ctx: ToolContext
    ) -> ToolResult:
        if not isinstance(message, str) or not message.strip():
            return _fail("Committing needs 'message'.")
        message = message.strip()
        if len(message) > COMMIT_MESSAGE_MAX:
            return _fail(f"The commit message is longer than {COMMIT_MESSAGE_MAX} characters.")
        if len(message.splitlines()[0]) > COMMIT_SUBJECT_MAX:
            return _fail(
                f"The first line of the commit message is longer than {COMMIT_SUBJECT_MAX} "
                "characters. Shorten it and put the details after a blank line."
            )
        user = self._account(ctx.conn)
        if user is None:
            return _fail(NO_ACCOUNT)
        git = self._repos.git
        await git.run(clone, "add", "-A", timeout=GIT_TIMEOUT_S)
        staged = await git.run(clone, "diff", "--cached", "--quiet", check=False)
        if staged.code == 0:
            return _fail("There is nothing to commit.")
        await git.run(
            clone,
            "-c", f"user.name={user.login}",
            "-c", f"user.email={noreply_email(user)}",
            "-c", "commit.gpgsign=false",
            "commit", "--quiet", "--no-verify", "-m", message,
            timeout=GIT_TIMEOUT_S,
        )  # fmt: skip
        head = await git.run(clone, "rev-parse", "--short", "HEAD")
        return ToolResult(ok=True, content=f"Committed {head.out.strip()} on {branch}.")
