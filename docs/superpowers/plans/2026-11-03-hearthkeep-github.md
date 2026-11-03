# Hearthkeep Phase 4: GitHub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner connect GitHub with a fine-grained token, add their own repos, and have the local model read, search, edit, branch, commit and test them, with every push, pull request, comment, merge or close waiting on an approval card that shows the exact diff and needs a passkey.

**Architecture:** Repos are cloned into `<data>/workspace/repos/<repo-id>/` and driven by a hardened `git` on the host that never runs hooks and never sees the token in its argv or config. Reading is Look, editing the clone is Change local (approval shows the diff), and everything that reaches GitHub is Outward. Tests run in a second sandbox image, on a throwaway copy of the working tree with no `.git` and no network; dependencies are installed once per lock-file hash, in a container that has network only after an Outward approval, into a cached Docker volume. GitHub's REST API is called with `httpx`, never through MCP.

**Tech Stack:** Phase 1–3 stack (Python 3.12+/uv, FastAPI, httpx, pytest; React + TS, Vitest; Docker Desktop). Host `git` ≥ 2.31 (for `GIT_CONFIG_COUNT`). New sandbox image `hearthkeep-repo`: Python 3.12, Node 22, uv. No new server or web runtime dependency.

**Spec:** `docs/design.md` §8.2 (permission engine), §8.3 *GitHub*, §9.4 (secrets), §9.6 (prompt injection), §10 (Repos view). No separate Phase 4 spec: the owner chose to skip the design questions, so the calls this plan makes on its own are listed under **Calls made in this plan** for review. Earlier plans' Global Constraints still apply (`2026-10-26-hearthkeep-foundation.md`, `-models-images.md`, `-files-analysis.md`).

## Global Constraints

- **Token:** fine-grained personal access token only, matching `github_pat_[A-Za-z0-9_]{20,250}`; stored in the secret store as `github_token`; checked with `GET /user` before it is saved. It never appears in argv, environment of any container, `.git/config`, the remote URL, the audit log, server logs, model context, tool results or error text (redacted as `[redacted]`, also its Basic-auth base64 form).
- **Access widening needs a passkey:** step-up purposes gain `github_token` (save or replace the token) and `add_repo`. Removing a repo deletes its clone, so it uses purpose `delete` with target `repo:<id>`. Removing the token is narrowing: session only.
- **GitHub API:** base `https://api.github.com` only; `httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=20)`; headers `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `User-Agent: Hearthkeep`. Paths allowed: `/user` and `/repos/{full_name}/…` for an added repo. Response bodies trimmed before they reach the model (lists ≤ 30 items, any text field ≤ 4,000 chars).
- **Repo names** (`owner/name`) are personal: shown to the owner's session and the model, never in the audit log or server logs (audit records the repo id). Name rule: `[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}`, the name part not `.` or `..`, compared case-insensitively.
- **Host git, every call:** minimal environment (`PATH`, `SYSTEMROOT` on Windows, `HOME`/`USERPROFILE` pointed at `<data>/runtime/git-home`), plus `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=<data>/runtime/git-home/empty.gitconfig`, `GIT_ASKPASS=` and these exact `-c` flags: `core.hooksPath=<data>/runtime/git-home/no-hooks`, `core.fsmonitor=false`, `core.autocrlf=false`, `core.symlinks=false`, `core.longpaths=true`, `protocol.allow=never`, `protocol.https.allow=always`, `submodule.recurse=false`, `credential.helper=`, `http.followRedirects=false`. The remote URL is always passed explicitly as `https://github.com/<full_name>.git`, never read from `.git/config`. The token, when a command needs it (clone, fetch, push), goes in `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=http.https://github.com/.extraheader`, `GIT_CONFIG_VALUE_0=AUTHORIZATION: basic <base64("x-access-token:"+token)>`. Submodules are never fetched.
- **Paths the model gives:** relative POSIX, ≤ 400 chars, no empty, `.` or `..` component, no component equal to `.git` case-insensitively after stripping trailing dots and spaces (so `.GIT`, `.git.` and `.git ` are refused), no `:` (NTFS streams), no 8.3 name `GIT~1`; the resolved path must stay inside the clone with no symlink or junction on the way. Refusal copy: `That path is outside the repo or inside its .git folder.`
- **Branches:** the model works only on branches named `hearthkeep/<slug>`, slug `[a-z0-9][a-z0-9-]{0,39}`; edits and commits on any other branch are refused (`Create a hearthkeep/ branch first with repo_edit action "branch".`). Push is never forced, and only to the same-named branch on GitHub. The model can never push to the default branch.
- **Commit identity:** author and committer `<login> <<user_id>+<login>@users.noreply.github.com>` from the checked token's account. Commit message ≤ 2,000 chars, first line ≤ 72.
- **Action classes and level keys:** `repo_read` Look (`github`); `repo_edit` Change local (`github`); `repo_install` Outward when the dependencies for the current lock hash are not cached, else Look (`github`); `repo_test` Look, sandboxed (`github_tests`, already in `SANDBOXED_TOOLS`); `github` Look for `list_prs`, `get_pr`, `list_issues`, `get_issue`, `checks`, Outward for `push`, `open_pr`, `comment`, `merge_pr`, `close`. Defaults: `github` Ask (unchanged), `github_tests` Ask. Without host git or without a saved token, all five tools are not offered (treated as Off).
- **Approval detail shows what will happen:** edits and every Outward action carry a unified diff in `detail.diff` (capped at 200 KB, then `… diff cut at 200 KB`), plus `detail.head_sha` for anything that publishes commits. The tool re-checks `head_sha` when it runs and refuses if it changed: `The branch changed after you approved it. Nothing was pushed.`
- **Repo test sandbox (exact):** image `hearthkeep-repo:<12-hex content hash>`; one container per run: `--rm --pull never --log-driver none --network none --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --memory 4g --memory-swap 4g --cpus 2 --pids-limit 512 --tmpfs /tmp:rw,nosuid,nodev,size=1073741824`, a copy of the working tree (no `.git`) bind-mounted read-write at `/repo`, the dependency volume read-only (Python at `/deps`, Node at `/repo/node_modules`), `HOME=/tmp`, `CI=1`. Limit **600 s**; output kept as the first 64 KB plus the last 192 KB with `… output cut …` between. Tree copy refused over 500 MB or 50,000 files (skipping `.git` and `node_modules`).
- **Dependency install (Outward):** same flags but default bridge network, `--pids-limit 1024`, limit **900 s**, only the dependency files mounted read-only at `/src`, the volume `hk-deps-<repo-id>-<hash12>` read-write. Commands, detected in this order: `uv.lock` → `uv sync --frozen` into `/deps/venv`; `requirements.txt` (+ `requirements-dev.txt` if present) → `python -m venv /deps/venv && /deps/venv/bin/pip install -r …`; `package-lock.json` → `npm ci` with `node_modules` as the volume. Lock hash = sha256 over ecosystem, image tag and each dependency file's name and bytes. Older volumes of that repo are removed after a successful install.
- **Not in this phase:** GitHub's MCP server, GitHub Enterprise, organisation-wide tokens, submodules, Git LFS, force push, rebasing, editing issues/PR bodies after posting, GitHub Actions logs, GPU in the sandbox, ecosystems other than uv, pip and npm.

## Review Focus

1. **The model edits again, or a commit lands, after the push card is shown** → the push refuses with the head-changed message and nothing reaches GitHub. (Task 7: `test_push_refuses_when_head_moved_after_approval`.)
2. **A path like `.GIT/hooks/post-checkout`, `src/../.git/config`, `.git./x` or a symlinked folder pointing outside the clone** → refused by read, write and replace. A test run that writes into `.git` or plants a hook can't touch the host clone, because tests run on a copy without `.git`. (Task 2: `test_safe_path_refuses_git_dir_spellings`; Task 6: `test_test_run_cannot_reach_host_clone`.)
3. **git fails with the remote URL and auth header in its error output** → the message reaching the model, the audit log and the approval card has `[redacted]` in place of the token and its base64. (Task 2: `test_git_errors_never_show_token`.)
4. **The lock file changes after dependencies were installed** → `repo_install` is Outward again and `repo_test` says the dependencies are out of date; with no change, `repo_install` is Look and runs without asking. (Task 5: `test_lock_change_needs_new_approval`.)
5. **A test suite that hangs or prints without end** → stopped at 600 s, the container removed, output cut with the head and tail kept, the run saved as timed out, and the chat carries on. (Task 6: `test_hanging_tests_stop_and_keep_tail`.)

## Calls made in this plan

These were not decided by the owner; check them while reading:

- Edits only on `hearthkeep/<slug>` branches, never pushing to the default branch, never force.
- Commits use the GitHub noreply address of the token's account, so no email goes into config.
- `github_tests` defaults to **Ask** (the design starts strict); the owner can set it to Allowed.
- Removing a repo needs a passkey, because it deletes unpushed local work.
- Ecosystems in this phase: uv, pip and npm only. Install scripts (npm `postinstall`) do run, inside the install container, which sees only the dependency files.
- The GitHub token panel lives in Settings; the repo list, add and remove live in the Repos view.

---

## File structure

```
server/src/hearthkeep/
  github/__init__.py
  github/api.py        # GitHubClient, GitHubError, token rule
  github/git.py        # GitRunner: hardened host git, token via GIT_CONFIG_*
  github/paths.py      # safe_path, branch and name rules
  github/repos.py      # RepoStore: add/clone, remove, fetch, status
  github/diff.py       # unified diffs for approval cards, size cap
  github/deps.py       # DepsCache: detect ecosystem, lock hash, install container
  github/testrun.py    # RepoTestRunner: tree copy, test container, repo_runs rows
  tools/repo.py        # repo_read, repo_edit
  tools/repo_deps.py   # repo_install, repo_test
  tools/github.py      # github (Look + Outward actions)
  sandbox/repo_image/Dockerfile
  api/routes_github.py
web/src/api/github.ts
web/src/components/DiffView.tsx GitHubPanel.tsx
web/src/views/Repos.tsx
web/src/styles/repos.css
```

---

### Task 1: GitHub account, API client and token settings

**Files:**
- Create: `server/src/hearthkeep/github/__init__.py`, `github/api.py`, `server/src/hearthkeep/api/routes_github.py`
- Modify: `server/src/hearthkeep/db.py` (migration 9), `api/routes_auth.py` (`StepUpPurpose` gains `github_token`, `add_repo`; `delete` accepts target `repo:<id>`), `api/common.py` (`Services.github: GitHubClient | None`), `api/app.py` (router, client), `server/tests/fakes.py` (`FakeGitHub`)
- Test: `server/tests/test_github_api.py`, `server/tests/test_api_github.py`

**Interfaces:**
- Produces: migration 9, `user_version` = 9: `github_account(id INTEGER PRIMARY KEY CHECK (id = 1), login TEXT, user_id INTEGER, checked_at TEXT)`; `repos(id TEXT PRIMARY KEY, full_name TEXT UNIQUE COLLATE NOCASE, default_branch TEXT, private INTEGER, created_at TEXT)`; `repo_deps(repo_id TEXT, lock_hash TEXT, ecosystem TEXT, volume TEXT, installed_at TEXT, PRIMARY KEY (repo_id, lock_hash))`; `repo_runs(id TEXT PRIMARY KEY, repo_id TEXT, chat_id TEXT, command TEXT, status TEXT, exit_code INTEGER, duration_ms INTEGER, output TEXT, created_at TEXT)`; index `repo_runs_repo ON repo_runs(repo_id, created_at)`.
- Produces (`api.py`): `TOKEN_SECRET = "github_token"`; `TOKEN_RE = re.compile(r"github_pat_[A-Za-z0-9_]{20,250}")`; `class GitHubError(Exception)` with `status: int | None`, `reason: Literal["unauthorized","not_found","forbidden","rate_limited","unavailable","refused"]`, `message: str` (owner-facing copy, never the token); `@dataclass(frozen=True) GitHubUser(login: str, id: int)`; `@dataclass(frozen=True) RepoInfo(full_name: str, default_branch: str, private: bool, can_push: bool)`; `class GitHubClient: __init__(token: Callable[[], str | None], transport: httpx.AsyncBaseTransport | None = None)`; `async user(token: str | None = None) -> GitHubUser` (an explicit token checks one not yet saved); `async repo(full_name: str) -> RepoInfo`; `async request(method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> Any` (path checked against the allowed set; `refused` otherwise); `noreply_email(user: GitHubUser) -> str` returns `f"{user.id}+{user.login}@users.noreply.github.com"`.
- Copy: unauthorized `GitHub didn't accept the token. Check it hasn't expired, then save it again.`; not_found `GitHub can't find that repo, or the token can't see it.`; rate_limited `GitHub says to slow down. Try again in a few minutes.`; unavailable `Can't reach GitHub right now.`
- Routes (session; CSRF on writes): `GET /api/github` → `{"git": bool, "connected": bool, "login": str | null}`; `PUT /api/github/token` `{token, step_up_token}` (purpose `github_token`) → 422 `token_format` / 422 `token_rejected` / 200 `{"connected": true, "login"}`, audit `github.token_saved` with no token data; `DELETE /api/github/token` → removes secret and account row, audit `github.token_removed`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_client_sends_headers_and_no_env_proxy(fake_github, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    client = GitHubClient(lambda: "github_pat_" + "a" * 30, transport=fake_github.transport)
    assert await client.user() == GitHubUser("octo", 42)
    sent = fake_github.requests[-1]
    assert sent.headers["x-github-api-version"] == "2022-11-28"
    assert sent.headers["authorization"] == "Bearer github_pat_" + "a" * 30

async def test_request_refuses_paths_outside_added_repos(client):
    with pytest.raises(GitHubError) as e:
        await client.request("GET", "/orgs/acme/repos")
    assert e.value.reason == "refused"

async def test_error_message_never_contains_token(client, fake_github):
    fake_github.fail_next(401, body={"message": "Bad credentials github_pat_" + "a" * 30})
    with pytest.raises(GitHubError) as e:
        await client.user()
    assert "github_pat_" not in e.value.message

def test_put_token_needs_step_up(api_client):          # 403 step_up_required, nothing stored
def test_put_token_bad_format_is_422(api_client)        # reason token_format
def test_put_token_rejected_by_github_is_422(api_client)  # reason token_rejected, secret absent
def test_put_token_saves_and_get_shows_login(api_client)  # GET → connected true, login "octo"
def test_delete_token_is_session_only(api_client)       # no step-up; secret and row gone
def test_audit_never_holds_token(api_client)            # no audit row contains "github_pat_"
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_github_api.py tests/test_api_github.py -q`
Expected: FAIL (`ModuleNotFoundError: hearthkeep.github`)

- [ ] **Step 3: Implement migration 9, `github/api.py`, the step-up purposes and `routes_github.py`**

`FakeGitHub` in `fakes.py` is an `httpx.MockTransport` with a dict of `(method, path) -> (status, json)`, `requests: list[httpx.Request]` and `fail_next(status, body)`. Redact the token from any GitHub message before building `GitHubError`.

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check .`
Expected: PASS, no lint findings

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(github): token settings with passkey step-up and a GitHub API client"
```

---

### Task 2: Hardened git, safe paths and the repo store

**Files:**
- Create: `server/src/hearthkeep/github/git.py`, `github/paths.py`, `github/repos.py`
- Modify: `api/routes_github.py` (repo routes), `api/common.py` (`Services.repos: RepoStore | None`), `api/app.py`, `config.py` (`DATA_SUBDIRS` gains `runtime/git-home/no-hooks`)
- Test: `server/tests/test_git.py`, `server/tests/test_repo_paths.py`, `server/tests/test_api_repos.py`

**Interfaces:**
- Consumes: Task 1 `GitHubClient.repo`, `TOKEN_SECRET`, migration 9 `repos`.
- Produces (`git.py`): `MIN_GIT = (2, 31)`; `github_url(full_name: str) -> str`; `@dataclass(frozen=True) GitResult(code: int, out: str, err: str)`; `class GitError(Exception)` with `message: str` (already redacted); `class GitRunner: __init__(data_dir: Path, git: str = "git", remote_url: Callable[[str], str] = github_url, protocol: str = "https")`; `available() -> bool` (cached `git --version` ≥ `MIN_GIT`); `async run(cwd: Path, *args: str, token: str | None = None, timeout: float = 60.0, check: bool = True) -> GitResult` (raises `GitError` on non-zero when `check`; kills the process tree on timeout); `argv(*args) -> list[str]` and `env(token: str | None) -> dict[str, str]` exposed for tests. `protocol` and `remote_url` exist so tests can use a local bare repo over `file`.
- Produces (`paths.py`): `class PathRefused(Exception)` with `message: str`; `safe_path(root: Path, rel: str) -> Path`; `BRANCH_PREFIX = "hearthkeep/"`; `BRANCH_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")`; `REPO_NAME = re.compile(...)` (Global Constraints) and `check_repo_name(full_name: str) -> str` (raises `ValueError`).
- Produces (`repos.py`): `@dataclass(frozen=True) Repo(id: str, full_name: str, default_branch: str, private: bool, created_at: str)`; `@dataclass(frozen=True) RepoStatus(branch: str, changed: list[str], ahead: int, behind: int)`; `class RepoError(Exception)` with `reason: Literal["no_token","name_format","repo_not_found","already_added","clone_failed"]`, `message: str`; `class RepoStore: __init__(conn, data_dir: Path, git: GitRunner, api: GitHubClient, secrets: SecretStore)`; `list() -> list[Repo]`; `get(repo_id: str) -> Repo | None`; `by_name(full_name: str) -> Repo | None`; `dir(repo: Repo) -> Path` (`<data>/workspace/repos/<id>`); `token() -> str | None`; `async add(full_name: str) -> Repo` (check name, `api.repo`, clone `--no-recurse-submodules` into `runtime/` then move into place, insert row; on any failure no row and no folder); `async remove(repo_id: str) -> bool` (folder, `repo_runs`, `repo_deps` rows; deps volumes are removed by Task 5's hook `on_remove: list[Callable[[Repo], Awaitable[None]]]`); `async fetch(repo: Repo) -> None` (`git fetch --prune <url> +refs/heads/*:refs/remotes/origin/*` with token); `async status(repo: Repo) -> RepoStatus`.
- Routes: `GET /api/repos` → `{"repos": [{id, full_name, default_branch, private, branch, changed, ahead, behind, deps: null, last_run: null}]}` (Tasks 5 and 6 fill `deps` and `last_run`); `POST /api/repos` `{full_name, step_up_token}` (purpose `add_repo`) → 201 repo JSON, or 409 `no_token`, 422 `name_format`, 404 `repo_not_found`, 409 `already_added`, 502 `clone_failed`; `DELETE /api/repos/{repo_id}` `{step_up_token}` (purpose `delete`, target `repo:<id>`); `POST /api/repos/{repo_id}/fetch`. Audit `repos.added`, `repos.removed`, `repos.fetched` with the id only.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("rel", [".git/config", ".GIT/hooks/x", ".git./x", ".git /x",
                                 "a/../.git/HEAD", "../outside", "/abs", "C:/x", "a:stream",
                                 "GIT~1/config", "", "a//b"])
def test_safe_path_refuses_git_dir_spellings(tmp_path, rel):
    with pytest.raises(PathRefused):
        safe_path(tmp_path, rel)

def test_safe_path_refuses_symlinked_folder(tmp_path)   # link inside root → outside: refused

def test_git_argv_has_every_hardening_flag(runner):
    argv = runner.argv("status")
    for flag in ("core.hooksPath=", "core.fsmonitor=false", "protocol.allow=never",
                 "submodule.recurse=false", "credential.helper=", "core.autocrlf=false"):
        assert any(a.startswith(flag) for a in argv)

def test_token_is_in_env_not_argv(runner):
    token = "github_pat_" + "b" * 30
    assert all(token not in a for a in runner.argv("fetch"))
    assert runner.env(token)["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"

async def test_git_errors_never_show_token(runner, tmp_path):
    # a fetch from a missing remote fails; stderr echoes config: both forms must be redacted
    with pytest.raises(GitError) as e:
        await runner.run(tmp_path, "fetch", "file:///nope", token="github_pat_" + "b" * 30)
    assert "github_pat_" not in e.value.message and "eC1hY2Nlc3MtdG9rZW4" not in e.value.message

async def test_hooks_in_clone_never_run(local_origin, store)  # planted .git/hooks/post-checkout doesn't run
async def test_add_clones_and_remove_deletes(local_origin, store)
async def test_add_failure_leaves_no_row_or_folder(store, fake_github)
def test_add_repo_needs_step_up(api_client)
def test_remove_repo_needs_delete_step_up_bound_to_repo(api_client)  # a token for repo:other fails
def test_audit_has_repo_id_never_name(api_client)
```

`local_origin` is a fixture that makes a bare repo with two commits under `tmp_path` and a `RepoStore` whose `GitRunner` uses `protocol="file"` and `remote_url=lambda n: origin.as_uri()`.

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_git.py tests/test_repo_paths.py tests/test_api_repos.py -q`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement `paths.py`, `git.py`, `repos.py` and the routes**

Run git with `asyncio.create_subprocess_exec` (no shell), `CREATE_NO_WINDOW` on Windows (same `_spawn_flags` idea as `sandbox/runner.py`). `safe_path` walks each component with `os.lstat` and refuses `S_ISLNK` or a reparse point (`st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT` on Windows). Redaction replaces the token and the base64 of `x-access-token:<token>`.

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check .`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(github): hardened host git, safe repo paths and adding or removing repos"
```

---

### Task 3: Approval detail from tools, and `repo_read`

**Files:**
- Create: `server/src/hearthkeep/tools/repo.py` (the `RepoReadTool` half)
- Modify: `tools/base.py`, `agent/loop.py`, `api/app.py` (register when git is available and a token is saved; re-check on each turn via `available`)
- Test: `server/tests/test_agent_loop.py` (new cases), `server/tests/test_repo_tools.py`

**Interfaces:**
- Produces (`base.py`): `ToolContext` gains `approved_detail: dict | None = None`. Optional tool method, looked up with `getattr`: `async approval_detail(self, args: dict, ctx: ToolContext) -> dict | None`. Optional `def is_available(self) -> bool` (missing → available); `ToolRegistry.available` drops tools that say False.
- Changes (`loop.py`): before creating an approval, call `approval_detail`; the card's detail is `{"tool", "arguments", **extra}` (redacted as now). If `approval_detail` raises, the call fails closed like a failed `classify`. After approval, `_run` builds the `ToolContext` with `approved_detail=resolved.detail`.
- Produces (`repo.py`): `class RepoReadTool` name `repo_read`, level `github`, Look. Args `{repo: str (owner/name), action: "list"|"read"|"search"|"status"|"diff"|"log"|"fetch", path?: str, start_line?: int ≥ 1, query?: str ≤ 200, ref?: str}`. Results: `list` ≤ 500 entries, two levels deep from `path`; `read` ≤ 400 lines from `start_line`, file ≤ 1 MB, binary (NUL in first 8 KB) refused; `search` = `git grep -n -I -F -e <query>` (≤ 200 matches, each line ≤ 300 chars); `status` = branch, changed files, ahead/behind; `diff` = working tree and commits since the merge base with `origin/<default>` (200 KB cap); `log` = last 20 commits one line each; `fetch` = `RepoStore.fetch`. Unknown repo: `That repo isn't added to Hearthkeep. Add it in Repos first.`

- [ ] **Step 1: Write the failing tests**

```python
async def test_approval_detail_is_shown_and_passed_back(loop_with_tool):
    # a fake Change-local tool whose approval_detail returns {"diff": "+x"}; level Ask
    events = await run_turn_and_approve(loop_with_tool)
    card = next(e for e in events if e["type"] == "approval_required")
    assert card["detail"]["diff"] == "+x"
    assert loop_with_tool.tool.seen_ctx.approved_detail["diff"] == "+x"

async def test_failing_approval_detail_fails_closed(loop_with_tool)   # no approval, step not ok
def test_unavailable_tool_is_not_offered(registry)

async def test_repo_read_lists_reads_and_searches(repo_fixture)
async def test_repo_read_refuses_binary_and_large(repo_fixture)
async def test_repo_read_unknown_repo_message(repo_fixture)
async def test_repo_read_search_query_is_not_an_option(repo_fixture)  # query "--open-files-in-pager=x" is literal
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_agent_loop.py tests/test_repo_tools.py -q`
Expected: FAIL

- [ ] **Step 3: Implement the base and loop changes, then `RepoReadTool`**

Pass the search query after `-e` and paths after `--` so nothing the model writes is read as a git option.

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check .`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(github): tools can describe their approval, and repo_read reads repos"
```

---

### Task 4: `repo_edit`: branch, write, replace, delete and commit

**Files:**
- Create: `server/src/hearthkeep/github/diff.py`
- Modify: `server/src/hearthkeep/tools/repo.py` (`RepoEditTool`), `api/app.py`
- Test: `server/tests/test_repo_edit.py`

**Interfaces:**
- Consumes: Task 2 `safe_path`, `BRANCH_PREFIX`, `BRANCH_SLUG`, `RepoStore`; Task 1 `noreply_email`, `github_account` row; Task 3 `approval_detail`.
- Produces (`diff.py`): `DIFF_CAP = 200 * 1024`; `cap_diff(text: str) -> str`; `file_diff(rel: str, old: str | None, new: str | None) -> str` (`difflib.unified_diff`, `a/` and `b/` prefixes, `/dev/null` for added or deleted).
- Produces: `class RepoEditTool` name `repo_edit`, level `github`, Change local. Args `{repo, action: "branch"|"write"|"replace"|"delete"|"commit", name?: str (slug), path?: str, content?: str ≤ 512 KB, old?: str, new?: str, message?: str}`. `branch`: `git switch -c hearthkeep/<name> origin/<default>` (or switch to it if it exists). `write`: whole file, creating folders. `replace`: `old` must occur exactly once (copy: `The text to replace was found {n} times; it must match exactly once.`). `delete`: removes one file. `commit`: `git add -A` then `git commit` with the noreply identity from Global Constraints, message rules enforced. All but `branch` refuse unless the current branch starts with `hearthkeep/`. `approval_detail`: `{"repo", "branch", "path", "diff"}` for write/replace/delete, `{"repo", "branch", "message", "diff": <staged diff>}` for commit, `{"repo", "branch"}` for branch. Text files only: content is written UTF-8, keeping the file's existing line endings when it already has CRLF.

- [ ] **Step 1: Write the failing tests**

```python
async def test_edit_refused_on_default_branch(repo_fixture)       # copy names the branch action
async def test_branch_then_write_shows_diff_in_approval(repo_fixture):
    detail = await edit.approval_detail({"repo": R, "action": "write", "path": "a.txt",
                                         "content": "new\n"}, ctx)
    assert detail["diff"].startswith("--- a/a.txt\n+++ b/a.txt")
async def test_replace_must_match_once(repo_fixture)
async def test_commit_uses_noreply_identity(repo_fixture):
    assert author == "octo <42+octo@users.noreply.github.com>"
async def test_commit_message_rules(repo_fixture)               # 73-char first line refused
async def test_crlf_file_keeps_crlf(repo_fixture)
async def test_write_into_git_dir_refused(repo_fixture)         # ".GIT/config" → PathRefused copy
def test_cap_diff_marks_cut()
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_repo_edit.py -q`
Expected: FAIL

- [ ] **Step 3: Implement `diff.py` and `RepoEditTool`**

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check .`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(github): repo_edit works on hearthkeep/ branches and shows each diff"
```

---

### Task 5: The repo sandbox image, cached dependencies and `repo_install`

**Files:**
- Create: `server/src/hearthkeep/sandbox/repo_image/Dockerfile`, `server/src/hearthkeep/github/deps.py`, `server/src/hearthkeep/tools/repo_deps.py` (`RepoInstallTool`)
- Modify: `sandbox/image.py` (a second image: `REPO_IMAGE_DIR`, `REPO_IMAGE_NAME = "hearthkeep-repo"`), `cli.py` (`sandbox build` builds both; `sandbox status` shows both), `api/routes_github.py` (`deps` in `GET /api/repos`), `api/app.py`
- Test: `server/tests/test_repo_deps.py`, existing `test_cli.py`

**Interfaces:**
- Consumes: Task 2 `RepoStore` (`on_remove` hook), `safe_path`; `sandbox/image.py` `image_tag(image_dir)`, `docker_ok`, `image_present`, `build_image`.
- Dockerfile: base `python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70` (the same digest as the analysis image); Node 22 copied from `node:22-bookworm-slim@sha256:<digest>` and uv from `ghcr.io/astral-sh/uv:<version>@sha256:<digest>` (both looked up at implementation time, at least 14 days old); user `10001`; `WORKDIR /repo`; no entrypoint script (the command is passed as `sh -c`).
- Produces (`deps.py`): `@dataclass(frozen=True) DepsPlan(ecosystem: Literal["uv","pip","npm"], files: list[str], command: str, lock_hash: str, volume: str)`; `class DepsCache: __init__(conn, docker: Sequence[str], image_tag: str)`; `plan(repo: Repo, root: Path) -> DepsPlan | None` (None: nothing to install); `installed(repo: Repo, plan: DepsPlan) -> bool` (row in `repo_deps` and the volume exists, the volume check cached 30 s); `install_argv(plan: DepsPlan, src_dir: Path) -> list[str]`; `async install(repo: Repo, plan: DepsPlan, job_dir: Path) -> tuple[bool, str]` (output capped as for tests); `async remove_for(repo: Repo) -> None` (registered as `RepoStore.on_remove`); `state(repo: Repo, root: Path) -> Literal["none","needed","ready","outdated"]` for the Repos view.
- Produces: `class RepoInstallTool` name `repo_install`, level `github`; `classify` → Outward unless `installed`, then Look; `approval_detail` → `{"repo", "command", "files", "lock_hash", "network": "This install has internet access. Only the dependency files are given to it."}`; run when already installed → `Dependencies are already installed for this lock file.`; without Docker or the repo image it never installs on the host and returns the Phase 3 `NO_DOCKER_MESSAGE` / `NO_IMAGE_MESSAGE` wording, naming `hearthkeep sandbox build`.

- [ ] **Step 1: Write the failing tests**

```python
def test_plan_prefers_uv_then_pip_then_npm(tmp_path)
def test_lock_hash_changes_with_file_bytes_and_image(tmp_path)
def test_install_argv_has_network_but_no_repo_mount(cache, tmp_path):
    argv = cache.install_argv(plan, tmp_path)
    assert "--network" not in argv or argv[argv.index("--network") + 1] != "none"
    assert not any("target=/repo" in a and "node_modules" not in a for a in argv)
    assert "--cap-drop" in argv and "--read-only" in argv
async def test_lock_change_needs_new_approval(repo_fixture, cache):
    assert tool.classify(args) == ActionClass.LOOK          # after a recorded install
    (root / "package-lock.json").write_text('{"changed": true}')
    assert tool.classify(args) == ActionClass.OUTWARD
async def test_remove_repo_removes_its_volumes(repo_fixture, fake_docker)
def test_sandbox_build_builds_both_images(cli_runner, fake_docker)
```

`fake_docker` records argv lists and answers `volume inspect` / `volume rm` like the Phase 3 fake.

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_repo_deps.py tests/test_cli.py -q`
Expected: FAIL

- [ ] **Step 3: Write the Dockerfile, `deps.py`, `RepoInstallTool` and the CLI change**

- [ ] **Step 4: Run the tests, the whole server suite, and build the image once on the reference host**

Run: `cd server && uv run pytest -q && uv run ruff check . && uv run hearthkeep sandbox build --data <data>`
Expected: PASS, and `sandbox status` lists `hearthkeep-repo:<hash>` as present

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(github): repo sandbox image and dependencies installed once per lock file"
```

---

### Task 6: `repo_test`: tests in the sandbox on a throwaway copy

**Files:**
- Create: `server/src/hearthkeep/github/testrun.py`
- Modify: `server/src/hearthkeep/tools/repo_deps.py` (`RepoTestTool`), `api/routes_github.py` (`last_run` in `GET /api/repos`; `GET /api/repos/{repo_id}/runs` → last 20 runs without output; `GET /api/repo-runs/{run_id}` → one run with output), `api/app.py`, `sandbox/runner.py` (only if a helper such as `_stop` or `_mount_source` needs exporting; no change to analysis behaviour)
- Test: `server/tests/test_repo_test.py`

**Interfaces:**
- Consumes: Task 5 `DepsCache.plan/installed`, the repo image tag; Task 2 `RepoStore.dir`; migration 9 `repo_runs`.
- Produces (`testrun.py`): `TEST_TIMEOUT_S = 600`; `HEAD_BYTES = 65_536`; `TAIL_BYTES = 196_608`; `MAX_TREE_BYTES = 500 * 1024**2`; `MAX_TREE_FILES = 50_000`; `@dataclass RepoRun(id, repo_id, chat_id, command, status: Literal["passed","failed","timed_out","error"], exit_code: int | None, duration_ms: int, output: str, created_at)`; `class RepoTestRunner: __init__(conn, data_dir: Path, docker: Sequence[str], image_tag: str, deps: DepsCache)`; `copy_tree(src: Path, dest: Path) -> None` (skips `.git` and `node_modules`, never follows links, enforces the caps); `argv(run_id: str, tree: Path, plan: DepsPlan | None, command: str) -> list[str]`; `async run(repo: Repo, chat_id: str, command: str) -> RepoRun` (job folder `<data>/runtime/repo-jobs/<run-id>/`, always removed; the container is named `hk-repo-<run-id>` with label `hearthkeep.repo=1` and killed on timeout); `runs(repo_id: str, limit: int = 20) -> list[RepoRun]`; `get(run_id: str) -> RepoRun | None`.
- Produces: `class RepoTestTool` name `repo_test`, level `github_tests`, Look. Args `{repo, command?: str ≤ 500}`; default command: `uv run pytest -q` (uv), `/deps/venv/bin/python -m pytest -q` (pip), `npm test` (npm), else asks the model to pass one. When dependencies are `needed` or `outdated` it returns, without running: `Dependencies aren't installed for the current lock file. Call repo_install first.` Result to the model: status, exit code, duration and the capped output.

- [ ] **Step 1: Write the failing tests**

```python
def test_argv_is_locked_down(runner, tmp_path):
    argv = runner.argv("r1", tmp_path, plan, "npm test")
    assert argv[argv.index("--network") + 1] == "none"
    for flag in ("--read-only", "--cap-drop", "--security-opt", "--rm"):
        assert flag in argv
    assert not any("/.git" in a for a in argv)
def test_copy_tree_skips_git_and_refuses_links(tmp_path)
def test_copy_tree_caps(tmp_path)                        # 50,001 files → refused with copy
async def test_test_run_cannot_reach_host_clone(repo_fixture, docker_or_skip)  # test writes .git/hooks/x and ../x; host clone unchanged
async def test_hanging_tests_stop_and_keep_tail(repo_fixture, docker_or_skip):
    run = await runner.run(repo, chat, "python -c \"import time\nwhile 1: print('x'*1000)\"")
    assert run.status == "timed_out" and "… output cut …" in run.output
async def test_missing_deps_message(repo_fixture)
```

`docker_or_skip` skips when Docker or the repo image is absent (CI on Linux has neither), like the Phase 3 sandbox tests; the time limit is patched down to 5 s in that test.

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_repo_test.py -q`
Expected: FAIL

- [ ] **Step 3: Implement `testrun.py` and `RepoTestTool`**

- [ ] **Step 4: Run the tests (with Docker on the reference host) and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check .`
Expected: PASS, the Docker tests not skipped on the reference host

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(github): repo_test runs a repo's tests in the sandbox on a throwaway copy"
```

---

### Task 7: The `github` tool: read PRs and issues, and approved push, PR, comment, merge, close

**Files:**
- Create: `server/src/hearthkeep/tools/github.py`
- Modify: `api/app.py`
- Test: `server/tests/test_github_tool.py`

**Interfaces:**
- Consumes: Task 1 `GitHubClient.request`; Task 2 `GitRunner`, `RepoStore`, `BRANCH_PREFIX`; Task 3 `approval_detail`, `ctx.approved_detail`; Task 4 `cap_diff`.
- Produces: `class GitHubTool` name `github`, level `github`. Args `{repo, action, number?: int, branch?: str, title?: str ≤ 256, body?: str ≤ 20,000, method?: "merge"|"squash", kind?: "pr"|"issue", ref?: str}`. Look: `list_prs` (open, ≤ 30), `get_pr` (title, body, state, head, base, changed files ≤ 100 with additions/deletions), `list_issues` (open, ≤ 30, PRs excluded), `get_issue` (with ≤ 30 latest comments), `checks` (check runs and combined status for `ref`). Outward, each with `approval_detail`:
  - `push`: branch must be `hearthkeep/*` and checked out; detail `{"repo", "branch", "head_sha", "commits": [one line each, ≤ 50], "diff": diff of origin/<branch> (or merge base with origin/<default>) to HEAD}`; run: refuse if HEAD ≠ approved `head_sha`, then `git push <url> <head_sha>:refs/heads/<branch>` (no `+`, no `--force`).
  - `open_pr`: head `hearthkeep/*` already on GitHub at the same sha as local HEAD (else `Push the branch first.`), base = default branch; detail `{"repo", "title", "body", "head", "base", "head_sha", "diff"}`; run re-checks the remote head sha before `POST /repos/{r}/pulls`.
  - `comment`: detail `{"repo", "number", "body"}`; `POST /repos/{r}/issues/{n}/comments`.
  - `merge_pr`: detail `{"repo", "number", "title", "method", "head_sha", "diff"}` (diff from `GET /pulls/{n}` with `Accept: application/vnd.github.diff`, capped); run calls `PUT /pulls/{n}/merge` with `sha=<approved head_sha>` so GitHub itself refuses if the PR moved.
  - `close`: detail `{"repo", "kind", "number", "title"}`; `PATCH` state `closed`.
- Every Outward run is audited as `tool.run` (as now) plus `github.<action>` with repo id and number only.

- [ ] **Step 1: Write the failing tests**

```python
def test_classify_per_action(tool)                       # list_prs Look, push Outward, unknown → error
async def test_push_detail_has_diff_and_head(repo_fixture)
async def test_push_refuses_when_head_moved_after_approval(repo_fixture):
    detail = await tool.approval_detail(args, ctx)
    await edit_and_commit(repo_fixture)                  # HEAD moves after the card was made
    result = await tool.run(args, replace(ctx, approved_detail=detail))
    assert not result.ok and result.content.startswith("The branch changed after you approved it")
    assert local_origin_branch_absent()
async def test_push_never_forces_and_never_default_branch(repo_fixture)
async def test_open_pr_needs_pushed_head(repo_fixture, fake_github)
async def test_merge_sends_approved_sha(fake_github)
async def test_untrusted_pr_body_stays_data(fake_github) # body says "ignore previous instructions": returned as text, no tool call made
async def test_outward_actions_need_step_up_even_when_allowed(loop_with_github_allowed)
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_github_tool.py -q`
Expected: FAIL

- [ ] **Step 3: Implement `GitHubTool`**

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check .`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(github): github tool reads PRs and issues and publishes only what was approved"
```

---

### Task 8: Web: diffs on approval cards, the GitHub panel and the Repos view

**Files:**
- Create: `web/src/api/github.ts`, `web/src/components/DiffView.tsx`, `web/src/components/GitHubPanel.tsx`, `web/src/views/Repos.tsx`, `web/src/styles/repos.css`
- Modify: `web/src/components/ApprovalCard.tsx`, `web/src/views/Settings.tsx`, `web/src/views/ToolsPermissions.tsx` (`NAMES.github_tests = 'GitHub tests'`), `web/src/App.tsx` (route `repos` to `Repos` instead of the placeholder), `web/src/api/webauthn.ts` (purposes `github_token`, `add_repo`, `delete` target `repo:<id>`), `web/src/shell/views.ts` (Settings blurb mentions GitHub)
- Test: `web/src/__tests__/diff.test.tsx`, `web/src/__tests__/repos.test.tsx`, `web/src/__tests__/github.test.tsx`, existing `approval.test.tsx`

**Interfaces:**
- Consumes: the routes from Tasks 1, 2, 5 and 6.
- Produces (`github.ts`): `type GitHubState = {git: boolean; connected: boolean; login: string | null}`; `type RepoRow = {id; full_name; default_branch; private: boolean; branch: string; changed: string[]; ahead: number; behind: number; deps: 'none'|'needed'|'ready'|'outdated'; last_run: {id; status; created_at} | null}`; `getGitHub()`, `saveToken(token)`, `removeToken()`, `listRepos()`, `addRepo(fullName)`, `removeRepo(id)`, `fetchRepo(id)`, `listRuns(id)`, `getRun(runId)` (the step-up calls inside, as `FoldersPanel` does).
- `DiffView({diff}: {diff: string})`: one block per file with the file path as a heading, added lines green, removed red, hunk headers dimmed, line numbers for both sides; the `… diff cut at 200 KB` line shown as a note; files folded when there are more than 5. Text only: never `dangerouslySetInnerHTML`.
- `ApprovalCard`: when `detail.diff` is a string, show the other detail fields as a short list (`Repo`, `Branch`, `Title`, `Command`, `Commits` …) and then `DiffView`; otherwise unchanged.
- `GitHubPanel` in Settings: not connected → token field (password input, `autocomplete="off"`), link to the install guide's token section, Save (passkey); connected → `Connected as @login`, Replace token (passkey), Remove (no passkey). Without host git: `Install Git to use GitHub with Hearthkeep.`
- `Repos` view: empty state with the add form; each repo row shows name, private badge, branch, changed files count, `ahead`/`behind`, dependencies state, last test run; actions Update (fetch), Remove (`ConfirmDelete` + passkey, copy `Removing deletes Hearthkeep's copy, including changes that were never pushed.`); a run opens its output in a panel. Phone layout: one column, actions in a menu.

- [ ] **Step 1: Write the failing tests**

```tsx
it('colours added and removed lines and labels each file', ...)   // getAllByRole('heading') paths; data-kind add/del
it('shows the cut note for a capped diff', ...)
it('approval card shows diff for push and keeps JSON for other tools', ...)
it('saving a token asks for a passkey and shows the login', ...)   // stepUp called with 'github_token'
it('removing the token needs no passkey', ...)
it('adding a repo asks for a passkey; bad name shows the server message', ...)
it('removing a repo confirms, warns about unpushed work, and binds the passkey to repo:<id>', ...)
it('shows dependencies state and last test run per repo', ...)
it('says to install Git when git is missing', ...)
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd web && npx vitest run src/__tests__/diff.test.tsx src/__tests__/repos.test.tsx src/__tests__/github.test.tsx src/__tests__/approval.test.tsx`
Expected: FAIL

- [ ] **Step 3: Implement the components, views and styles** (dark tokens from `tokens.css`; amber stays for approvals only)

- [ ] **Step 4: Run the web suite and build**

Run: `cd web && npm test && npm run build`
Expected: PASS, build without type errors

- [ ] **Step 5: Commit**

```bash
git add web
git commit -m "feat(web): diffs on approval cards, GitHub in Settings and the Repos view"
```

---

### Task 9: Docs, the end-to-end smoke test and the roadmap

**Files:**
- Modify: `docs/install.md` (Git requirement; "Add GitHub": create a fine-grained token with *Only select repositories*, permissions Contents read/write, Pull requests read/write, Issues read/write, Commit statuses read, Checks read, Metadata read; expiry advice; saving it and adding repos), `docs/security.md` (host git hardening, token handling, the two sandboxes, what the install container can reach), `docs/design.md` (§8.3 GitHub: the calls from this plan), `README.md` (roadmap Phase 4 ✅, the GitHub row in *What you can do with it*, Git in the requirements table), `scripts/smoke_e2e.py` (a GitHub section, skipped unless `HK_SMOKE_GITHUB_REPO` names a throwaway repo of the owner's)
- Test: `server/tests/test_docs.py` if it exists; otherwise the privacy scan

**Interfaces:**
- Consumes: everything above.
- Smoke steps against the real model and a throwaway repo: add the repo; ask the model to make a branch, change README, run the tests; approve the edit and the install; approve the push with a passkey; open a PR; check the PR on GitHub shows exactly the approved diff; close it.

- [ ] **Step 1: Update the docs and the smoke script**

- [ ] **Step 2: Run the privacy scan and both suites**

Run: `git ls-files -z | xargs -0 python scripts/privacy_scan.py && cd server && uv run pytest -q && cd ../web && npm test`
Expected: no findings, all PASS

- [ ] **Step 3: Run the smoke test on the reference host**

Run: `cd server && HK_SMOKE_GITHUB_REPO=<owner>/<throwaway> uv run python ../scripts/smoke_e2e.py --data <data>`
Expected: every GitHub step reported ok; the PR's diff matches the approved card

- [ ] **Step 4: Commit**

```bash
git add docs README.md scripts
git commit -m "docs: GitHub setup, its security model and Phase 4 in the roadmap"
```
