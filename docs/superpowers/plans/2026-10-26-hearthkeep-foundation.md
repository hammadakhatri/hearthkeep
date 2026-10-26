# Hearthkeep Phase 1: Secure Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working, secure Hearthkeep core: the owner signs in with a passkey, chats with the local chat model through a streaming agent loop that enforces the permission engine and approval flow, and manages tool permissions in a dark, premium web UI. The repo is public-safe from the very first commit.

**Architecture:** A Python (FastAPI) server bound to 127.0.0.1 owns auth, the permission engine, approvals, audit and the agent loop. It talks to an already-running OpenAI-compatible chat server (llama-server) and serves a React + TypeScript single-page app. All personal state lives in a data folder outside the repo.

**Tech Stack:** Python 3.12+ managed with `uv`; FastAPI, uvicorn, httpx, py_webauthn (`webauthn`), keyring, cryptography, pytest, pytest-asyncio, ruff. Web: Vite, React 18, TypeScript, `@simplewebauthn/browser`, Vitest, Testing Library, `@fontsource/geist-sans`, `@fontsource/geist-mono`.

**Spec:** `docs/design.md` (public, scrubbed copy written in Task 1). The owner's full private design lives in the data folder at `<data>/private/hearthkeep-design.md`; executors with access should read both. Interface reference: the owner's "Hearthkeep interface draft 2" (private link held by the owner).

## Phase roadmap (this plan is Phase 1)

| Phase | Delivers | Depends on |
|---|---|---|
| **1. Secure core** (this plan) | Repo + privacy guard, config/data folder, DB/audit, secrets, permission engine, approvals, passkey auth, chat provider, agent loop, API, web shell with Chat + approvals + Tools & permissions | — |
| 2. Models & images | Model cards, model switchboard, process supervisor, GPU scheduler, ComfyUI provider, image tool + Images view | 1 |
| 3. Files & Python | Files tool + send-to-device links + Files view; Docker Python sandbox + Analysis view | 1 |
| 4. GitHub | Repo registry, clone/read, per-repo dependency approval and cached env, sandboxed tests, approved push/PR + Repos view | 1, 3 |
| 5. Briefing | Wallet MCP client (after verifying sign-in), RSS news, SearXNG search/fetch, scheduled briefing, memory, Today view with card order/visibility settings | 1, 2 |
| 6. Release | One-command installer, full installation guide, Tailscale setup, run-at-startup, sidebar customisation, keyboard shortcuts + command palette, audit log view, security review, v0.1 tag | 1–5 |

Each phase gets its own plan, written when the previous phase is done.

## Global Constraints

- Every server socket binds to `127.0.0.1` only; the server refuses to start if configured with any other host.
- Nothing personal in the repo: no absolute user paths, names, account or repo names, tokens, chats, images, claude.ai links. Personal values come only from the data folder.
- License: Apache 2.0. Public GitHub repo.
- Interface is always dark. Tokens: background `#000000`, surfaces `#0e0e10` / `#161618` / `#1f1f22`, line `#2a2a2e`, text `#f5f5f7`, secondary `#c7c7cc`, muted `#86868b`, accent `#2997ff`, amber `#ffb340`, green `#32d74b`, red `#ff6961`; fonts Geist / Geist Mono. Respect `prefers-reduced-motion`.
- Action classes: **Look**, **Change local**, **Outward or permanent**. Tool levels: **Allowed / Ask / Off**.
- Outward or permanent actions always require an approval card plus a fresh passkey assertion, regardless of tool level.
- The model has no tool that changes Hearthkeep settings, permissions, model cards or code, and cannot resolve approvals.
- Owner settings changes that widen access require passkey step-up; narrowing needs only a session.
- v1 default levels: wallet, news, files, python, images = Allowed; github = Ask. Python (and repo test runs) are forced to Ask when Docker is unavailable.
- Secrets never appear in prompts, logs or audit details.
- Python package import name `hearthkeep`; CLI command `hearthkeep`.

## Review Focus

1. **Two devices approve the same request at once, or an approval is answered after it expired** → exactly one resolution wins; late or duplicate answers get a clear "already handled / expired" response and nothing runs twice. (Task 6: `test_concurrent_resolve_only_one_wins`, `test_resolve_after_expiry_rejected`.)
2. **The browser tab closes or the phone loses signal mid-reply** → the turn keeps running on the server and the full result is there when the owner reopens the chat. (Task 9: `test_turn_survives_subscriber_disconnect`.)
3. **The chat server is down or restarts mid-stream** → the turn ends with a plain-language error message in the chat, the server stays up, and the next message works. (Task 8: `test_stream_connection_error_raises_provider_error`; Task 9: `test_provider_failure_ends_turn_with_error_event`.)
4. **The model emits a malformed tool call** (invalid JSON, unknown tool, arguments that fail the schema) → it gets an error back as the tool result and can retry; no crash; it counts toward the step limit. (Task 9: `test_malformed_tool_call_returns_error_to_model`.)
5. **Config points the server at `0.0.0.0` or a public origin that isn't HTTPS** → the server refuses to start with a message explaining Tailscale is the only remote path. (Task 2: `test_non_loopback_host_rejected`, `test_public_origin_must_be_https`.)

---

## File structure

```
hearthkeep/
├─ LICENSE  README.md  SECURITY.md  CONTRIBUTING.md  config.example.toml
├─ .gitignore  .pre-commit-config.yaml  .github/workflows/ci.yml
├─ docs/design.md  docs/security.md  docs/superpowers/plans/…
├─ scripts/privacy_scan.py              # pre-commit + CI personal-data/secret scanner
├─ server/
│  ├─ pyproject.toml
│  ├─ src/hearthkeep/
│  │  ├─ config.py        # Settings, load/validate, init_data_dir
│  │  ├─ db.py            # connect + migrations (schema v1)
│  │  ├─ audit.py         # record/list audit events
│  │  ├─ secrets.py       # SecretStore (keyring-held key + Fernet file)
│  │  ├─ permissions.py   # ActionClass, ToolLevel, Decision, decide(), widening rules, defaults
│  │  ├─ approvals.py     # ApprovalStore
│  │  ├─ auth/
│  │  │  ├─ webauthn.py   # Verifier protocol + py_webauthn implementation
│  │  │  ├─ sessions.py   # SessionStore
│  │  │  └─ enrollment.py # enrollment codes
│  │  ├─ providers/chat.py   # OpenAIChatClient streaming
│  │  ├─ tools/base.py       # Tool protocol, ToolResult, ToolContext, registry
│  │  ├─ tools/demo.py       # clock (Look) + note (Change local) tools, used to exercise the loop in Phase 1
│  │  ├─ agent/loop.py       # AgentLoop, events
│  │  ├─ agent/runner.py     # TurnRunner (background turns, persisted events, subscribers)
│  │  ├─ api/app.py          # create_app
│  │  ├─ api/routes_auth.py  routes_chats.py  routes_approvals.py  routes_settings.py  routes_audit.py
│  │  └─ cli.py              # `hearthkeep serve`, `hearthkeep enroll-code`
│  └─ tests/…
├─ web/
│  ├─ package.json  vite.config.ts  tsconfig.json  index.html  public/manifest.webmanifest
│  └─ src/
│     ├─ styles/tokens.css  styles/base.css
│     ├─ api/client.ts  api/events.ts  api/webauthn.ts
│     ├─ shell/Sidebar.tsx  shell/TopBar.tsx  shell/Composer.tsx  shell/AppShell.tsx
│     ├─ views/Chat.tsx  views/ToolsPermissions.tsx  views/SignIn.tsx  views/Placeholder.tsx
│     ├─ components/ApprovalCard.tsx  components/WorkedSteps.tsx  components/TriToggle.tsx
│     └─ main.tsx  App.tsx
└─ scripts/smoke_e2e.py   # manual check against the real local chat model
```

---

### Task 1: Repository bootstrap and privacy guard

**Files:**
- Move (outside the repo, before `git init`): `docs/superpowers/specs/2026-10-26-hearthkeep-design.md` → `<data>/private/hearthkeep-design.md`
- Create: `docs/design.md`, `LICENSE`, `README.md`, `SECURITY.md`, `CONTRIBUTING.md`, `.gitignore`, `scripts/privacy_scan.py`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `server/pyproject.toml`
- Test: `server/tests/test_privacy_scan.py`

**Interfaces:**
- Produces: `scripts/privacy_scan.py` with `scan_text(text: str, extra_markers: list[str]) -> list[Finding]`, `Finding(rule: str, line: int, excerpt: str)`, CLI `python scripts/privacy_scan.py [paths...]` exiting 1 on findings; reads extra markers from `.hearthkeep-personal-markers` (gitignored, one per line) if present.

- [ ] **Step 1: Move the private design out of the repo folder** to `<data>/private/` and confirm `docs/superpowers/specs/` is empty.
- [ ] **Step 2: Write `docs/design.md`**: the private design rewritten generically. Replace personal specifics with examples: "reference host: Windows 11 + 16 GB NVIDIA GPU", example data folder `C:\Hearthkeep\data` written as `<data>`, "a budgeting app that offers an MCP server (e.g. Wallet by BudgetBakers)", currency examples generic, no claude.ai links, no owner name.
- [ ] **Step 3: Write the failing tests** in `server/tests/test_privacy_scan.py`:

Build the "bad" strings by concatenation so this test file (and this plan) never contain a literal match and don't trip the scanner themselves:

```python
USERS = "C:" + "\\Users\\"
LINK = "https://claude" + ".ai/" + "artifact/AbC123"

def test_flags_windows_user_path():
    assert any(f.rule == "abs-path" for f in scan_text("open " + USERS + "someone\\notes.txt", []))

def test_flags_claude_artifact_link():
    assert any(f.rule == "private-link" for f in scan_text("see " + LINK, []))

def test_flags_tokens():
    for s in ["ghp_" + "a" * 36, "github_pat_" + "a" * 40, "sk-local-" + "b" * 40]:
        assert scan_text(f"token = '{s}'", []), s

def test_flags_extra_marker_case_insensitive():
    assert any(f.rule == "personal-marker" for f in scan_text("by Jane Example", ["jane example"]))

def test_allows_placeholders():
    assert scan_text("data folder: <data>/workspace and C:\\Hearthkeep\\data", []) == []
```

- [ ] **Step 4: Run** `cd server && uv run pytest tests/test_privacy_scan.py -v`. Expected: FAIL (module missing).
- [ ] **Step 5: Implement `scripts/privacy_scan.py`.** Rules: `abs-path` = a drive letter followed by `:\Users\` (any case), or `/home/<name>/`, or `/Users/<name>/`; owner-specific drive paths (e.g. their install folder) go in the personal-markers file rather than a built-in rule; `private-link` = the claude.ai domain followed by an `/artifact/` or `/code/artifact/` path; `token` = `ghp_[A-Za-z0-9]{36}`, `github_pat_[A-Za-z0-9_]{40,}`, `sk-[A-Za-z0-9-]{20,}`, `-----BEGIN [A-Z ]*PRIVATE KEY-----`; `personal-marker` = extra markers. Make the test import it via a `conftest.py` path insert.
- [ ] **Step 6: Run the tests.** Expected: PASS.
- [ ] **Step 7: Add the repo files.** `.gitignore` excludes `data/`, `*-data/`, `.hearthkeep-personal-markers`, `config.toml`, `*.gguf`, `*.safetensors`, `node_modules/`, `web/dist/`, `.venv/`, `__pycache__/`, `.pytest_cache/`. `LICENSE` = Apache 2.0 full text, copyright "Hearthkeep contributors". `README.md` = name, one-line pitch "Your private AI, at home.", status "Phase 1 in progress", Apache badge. `SECURITY.md` = how to report privately. `CONTRIBUTING.md` = the no-personal-data rule and the scanner. `.pre-commit-config.yaml` runs `python scripts/privacy_scan.py` on staged files. `ci.yml`: privacy scan, gitleaks action, `uv run pytest`, `ruff check`, `npm ci && npm test && npm run build` in `web/`.
- [ ] **Step 8: Create `.hearthkeep-personal-markers`** locally with the owner's identifiers (ask the owner which names/handles/repo names to include), run `python scripts/privacy_scan.py $(git ls-files -co --exclude-standard)` on the whole tree. Expected: no findings.
- [ ] **Step 9: `git init`, install the pre-commit hook, commit.** `git commit -m "chore: bootstrap repo with privacy guard"`

### Task 2: Settings and data folder

**Files:**
- Create: `server/src/hearthkeep/config.py`, `config.example.toml`
- Test: `server/tests/test_config.py`

**Interfaces:**
- Produces: `class ConfigError(Exception)`; `@dataclass(frozen=True) class Settings: data_dir: Path; host: str = "127.0.0.1"; port: int = 8765; public_origin: str = "http://localhost:8765"; rp_id: str = "localhost"; chat_base_url: str = "http://127.0.0.1:8080/v1"; chat_model: str = "gemma-4-12b"; chat_api_key_secret: str = "chat_api_key"; session_idle_days: int = 14; approval_ttl_minutes: int = 15; max_agent_steps: int = 8; docker_available: bool | None = None`; `load_settings(data_dir: Path) -> Settings`; `detect_docker() -> bool` (runs `docker info` with a 5 s timeout; used when `docker_available` is None); `init_data_dir(data_dir: Path) -> None` (creates `secrets/`, `workspace/output/`, `workspace/repos/`, `logs/`, `private/`, and writes `config.toml` from the example if missing).

- [ ] **Step 1: Write the failing tests**

```python
def test_defaults_when_config_minimal(tmp_path): s = load_settings(init(tmp_path)); assert s.host == "127.0.0.1" and s.port == 8765
def test_non_loopback_host_rejected(tmp_path): write(tmp_path, 'host = "0.0.0.0"'); with pytest.raises(ConfigError, match="Tailscale"): load_settings(tmp_path)
def test_public_origin_must_be_https(tmp_path): write(tmp_path, 'public_origin = "http://myhost.example.com"'); with pytest.raises(ConfigError): load_settings(tmp_path)
def test_localhost_origin_may_be_http(tmp_path): write(tmp_path, 'public_origin = "http://localhost:8765"'); load_settings(tmp_path)
def test_rp_id_must_match_origin_host(tmp_path): write(tmp_path, 'public_origin = "https://box.tail1234.ts.net"\nrp_id = "other.ts.net"'); with pytest.raises(ConfigError): load_settings(tmp_path)
def test_init_creates_structure(tmp_path): init_data_dir(tmp_path); assert (tmp_path/"workspace"/"output").is_dir() and (tmp_path/"config.toml").exists()
```

- [ ] **Step 2: Run** `uv run pytest tests/test_config.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `config.py`** with `tomllib`. Loopback = `127.0.0.1`, `::1`, `localhost`; the error message names Tailscale (`tailscale serve`) as the only way to reach Hearthkeep remotely.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): settings and data folder"`

### Task 3: Database and audit log

**Files:**
- Create: `server/src/hearthkeep/db.py`, `server/src/hearthkeep/audit.py`
- Test: `server/tests/test_db.py`, `server/tests/test_audit.py`

**Interfaces:**
- Produces: `connect(path: Path) -> sqlite3.Connection` (WAL mode, `row_factory=sqlite3.Row`, foreign keys on, migrations applied; `user_version` = 1). Schema v1 tables: `credentials(id TEXT PK, public_key BLOB, sign_count INT, device_name TEXT, created_at TEXT, revoked_at TEXT)`, `sessions(id TEXT PK, credential_id TEXT, created_at TEXT, last_seen_at TEXT, revoked_at TEXT)`, `enrollment_codes(code_hash TEXT PK, purpose TEXT, expires_at TEXT, used_at TEXT)`, `chats(id TEXT PK, title TEXT, think INT, created_at TEXT)`, `chat_grants(chat_id TEXT, tool TEXT, PRIMARY KEY(chat_id, tool))`, `messages(id INTEGER PK, chat_id TEXT, role TEXT, content TEXT, tool_calls TEXT, tool_call_id TEXT, created_at TEXT)`, `turn_events(id INTEGER PK, chat_id TEXT, turn_id TEXT, seq INT, type TEXT, data TEXT, created_at TEXT)`, `approvals(id TEXT PK, chat_id TEXT, turn_id TEXT, tool TEXT, action_class TEXT, summary TEXT, detail TEXT, requires_step_up INT, status TEXT, created_at TEXT, expires_at TEXT, resolved_at TEXT, resolved_by_session TEXT)`, `tool_levels(tool TEXT PK, level TEXT)`, `audit(id INTEGER PK, at TEXT, actor TEXT, event TEXT, detail TEXT)`.
- Produces: `record(conn, actor: str, event: str, detail: dict) -> None`; `list_events(conn, limit: int = 100, query: str | None = None) -> list[dict]`. `record` drops keys named `token`, `secret`, `api_key`, `password`, `code` from `detail` (replacing values with `"[redacted]"`).

- [ ] **Step 1: Write the failing tests**: `test_connect_applies_schema_v1` (user_version == 1, tables exist), `test_connect_idempotent`, `test_record_and_list_newest_first`, `test_list_filters_by_query`, `test_record_redacts_secret_keys` (`record(conn,"owner","x",{"token":"abc","path":"p"})` → stored detail has `"[redacted]"` and `"p"`).
- [ ] **Step 2: Run** `uv run pytest tests/test_db.py tests/test_audit.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `db.py` (migrations as an ordered list of SQL scripts keyed by version) and `audit.py`. Timestamps are UTC ISO-8601.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): sqlite schema and audit log"`

### Task 4: Secret store

**Files:**
- Create: `server/src/hearthkeep/secrets.py`
- Test: `server/tests/test_secrets.py`

**Interfaces:**
- Produces: `class SecretStore: __init__(self, data_dir: Path, service: str = "hearthkeep"); put(name: str, value: str) -> None; get(name: str) -> str | None; delete(name: str) -> None; names() -> list[str]`. A random Fernet key lives in the OS keyring under (`service`, `"master-key"`, scoped by data-dir path hash); values live encrypted in `<data>/secrets/store.bin`.

- [ ] **Step 1: Write the failing tests** using an in-memory keyring backend fixture: `test_put_get_roundtrip`, `test_file_does_not_contain_plaintext` (`b"supersecret" not in store.bin bytes`), `test_get_missing_returns_none`, `test_delete`, `test_wrong_key_raises` (replace keyring key → `get` raises `SecretStoreError`).
- [ ] **Step 2: Run** `uv run pytest tests/test_secrets.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** with `keyring` + `cryptography.fernet`. Writes are atomic (temp file + replace).
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): encrypted secret store"`

### Task 5: Permission engine

**Files:**
- Create: `server/src/hearthkeep/permissions.py`
- Test: `server/tests/test_permissions.py`

**Interfaces:**
- Produces: `class ActionClass(StrEnum): LOOK="look"; CHANGE_LOCAL="change_local"; OUTWARD="outward"`; `class ToolLevel(StrEnum): ALLOWED="allowed"; ASK="ask"; OFF="off"`; `class Decision(StrEnum): RUN="run"; ASK="ask"; ASK_STEP_UP="ask_step_up"; DENY="deny"`; `DEFAULT_LEVELS: dict[str, ToolLevel] = {"wallet": ALLOWED, "news": ALLOWED, "files": ALLOWED, "python": ALLOWED, "images": ALLOWED, "github": ASK, "demo": ALLOWED}` (`demo` exists only for the Phase 1 demo tools and is removed with them); `SANDBOXED_TOOLS = {"python", "github_tests"}`; `effective_level(tool: str, configured: ToolLevel, docker_available: bool) -> ToolLevel`; `decide(level: ToolLevel, action: ActionClass, granted_for_chat: bool) -> Decision`; `is_widening(old: ToolLevel, new: ToolLevel) -> bool`; `get_level(conn, tool) -> ToolLevel` / `set_level(conn, tool, level) -> None` (falls back to `DEFAULT_LEVELS`, then `ASK` for unknown tools).

- [ ] **Step 1: Write the failing tests**: a parametrised full matrix

```python
@pytest.mark.parametrize("level,action,granted,expected", [
    (OFF, LOOK, False, DENY), (OFF, CHANGE_LOCAL, True, DENY), (OFF, OUTWARD, True, DENY),
    (ASK, LOOK, False, ASK), (ASK, CHANGE_LOCAL, True, ASK), (ASK, OUTWARD, True, ASK_STEP_UP),
    (ALLOWED, LOOK, False, RUN), (ALLOWED, CHANGE_LOCAL, False, ASK), (ALLOWED, CHANGE_LOCAL, True, RUN),
    (ALLOWED, OUTWARD, False, ASK_STEP_UP), (ALLOWED, OUTWARD, True, ASK_STEP_UP),
])
def test_decide_matrix(level, action, granted, expected): assert decide(level, action, granted) == expected
```

plus `test_python_forced_to_ask_without_docker` (`effective_level("python", ALLOWED, False) == ASK`, `"images"` unaffected, `OFF` stays `OFF`), `test_is_widening` (OFF→ASK, ASK→ALLOWED, OFF→ALLOWED true; reverse false; same false), `test_defaults_v1`, `test_unknown_tool_defaults_to_ask`.
- [ ] **Step 2: Run** `uv run pytest tests/test_permissions.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `permissions.py`.**
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): permission engine"`

### Task 6: Approvals

**Files:**
- Create: `server/src/hearthkeep/approvals.py`
- Test: `server/tests/test_approvals.py`

**Interfaces:**
- Consumes: `ActionClass`, `audit.record`.
- Produces: `@dataclass class Approval: id, chat_id, turn_id, tool, action_class, summary, detail: dict, requires_step_up: bool, status: Literal["pending","approved","declined","expired"], created_at, expires_at`; `class ApprovalError(Exception)` with `reason: Literal["not_found","already_resolved","expired","step_up_required"]`; `class ApprovalStore: __init__(conn, ttl_minutes: int, clock: Callable[[], datetime]); create(chat_id, turn_id, tool, action_class, summary, detail, requires_step_up) -> Approval; get(id) -> Approval; pending() -> list[Approval]; resolve(id, session_id: str, approve: bool, step_up_verified: bool) -> Approval; async wait(id) -> Approval` (resolves when status leaves pending; expired approvals resolve as `expired`). Resolution is a single `UPDATE … WHERE id=? AND status='pending'`; zero rows changed → `already_resolved` (or `expired`). Ids from `secrets.token_urlsafe(16)`. Every create/resolve is audited.

- [ ] **Step 1: Write the failing tests**: `test_create_pending`, `test_approve_with_step_up_when_required`, `test_step_up_required_rejected_without_verification` (reason `step_up_required`, status still pending), `test_decline_never_needs_step_up`, `test_resolve_after_expiry_rejected` (fake clock past TTL → reason `expired`, status `expired`), `test_concurrent_resolve_only_one_wins` (two threads with separate connections resolving the same id → exactly one returns, the other raises `already_resolved`), `test_wait_returns_on_resolve` (async), `test_resolutions_are_audited`.
- [ ] **Step 2: Run** `uv run pytest tests/test_approvals.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `approvals.py`.** `wait` polls the row every 0.5 s or uses an `asyncio.Event` registry fed by `resolve`; either is fine as long as the test passes and expiry is checked on each poll.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): approval store"`

### Task 7: Passkey auth, sessions and enrollment

**Files:**
- Create: `server/src/hearthkeep/auth/webauthn.py`, `auth/sessions.py`, `auth/enrollment.py`
- Test: `server/tests/test_auth.py`

**Interfaces:**
- Produces: `class Verifier(Protocol): registration_options(user_id: bytes) -> dict; verify_registration(response: dict, challenge: bytes) -> NewCredential; authentication_options(credential_ids: list[str]) -> dict; verify_authentication(response: dict, challenge: bytes, credential: StoredCredential) -> int (new sign count)`; `class PyWebAuthnVerifier(Verifier)` built from `Settings.rp_id`, `Settings.public_origin`, rp name "Hearthkeep", user verification **required**. Test double: `FakeVerifier` in `tests/fakes.py`.
- Produces: `class SessionStore: create(credential_id) -> str; validate(session_id) -> Session | None` (rejects revoked, revoked-credential, or idle > `session_idle_days`; updates `last_seen_at`); `revoke(session_id)`; `revoke_credential(credential_id)` (revokes its sessions).
- Produces: `class EnrollmentCodes: issue(purpose: Literal["first_device","add_device"], ttl_minutes: int) -> str` (8-char code from an unambiguous alphabet; only the hash is stored); `consume(code: str, purpose) -> bool` (single use, not expired); `first_device_allowed(conn) -> bool` (true only when no unrevoked credentials exist).
- Produces: `class StepUp: issue(session_id: str, approval_id: str | None, purpose: str) -> str` (token valid 60 s, single use); `check(token, session_id, approval_id, purpose) -> bool`.

- [ ] **Step 1: Write the failing tests**: `test_first_device_code_only_when_no_credentials`, `test_code_single_use`, `test_code_expires`, `test_code_stored_hashed`, `test_session_idle_expiry`, `test_revoking_credential_kills_sessions`, `test_step_up_token_bound_to_session_and_approval` (other session or other approval → False), `test_step_up_token_single_use_and_expires`.
- [ ] **Step 2: Run** `uv run pytest tests/test_auth.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the three modules. `PyWebAuthnVerifier` wraps `webauthn.generate_registration_options` / `verify_registration_response` / `generate_authentication_options` / `verify_authentication_response`; challenges are kept server-side keyed by a short-lived random id.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): passkeys, sessions, enrollment, step-up"`

### Task 8: Chat provider

**Files:**
- Create: `server/src/hearthkeep/providers/chat.py`
- Test: `server/tests/test_chat_provider.py`, fixtures `server/tests/fixtures/sse_*.txt`

**Interfaces:**
- Produces: event dataclasses `TextDelta(text: str)`, `ReasoningDelta(text: str)`, `ToolCallRequest(id: str, name: str, arguments_json: str)`, `StreamDone(finish_reason: str)`; `class ProviderError(Exception)` with a plain-language `user_message`; `class OpenAIChatClient: __init__(base_url: str, api_key: str, model: str, transport: httpx.AsyncBaseTransport | None = None); async stream(messages: list[dict], tools: list[dict], think: bool) -> AsyncIterator[event]`. Sends `stream: true` and `chat_template_kwargs: {"enable_thinking": think}`; reads `delta.content`, `delta.reasoning_content`, and accumulates `delta.tool_calls` fragments by index into complete `ToolCallRequest`s emitted before `StreamDone`.

- [ ] **Step 1: Write the failing tests** with `httpx.MockTransport` serving SSE fixtures: `test_streams_text_deltas`, `test_streams_reasoning_separately`, `test_assembles_fragmented_tool_call` (name + arguments split over 4 chunks → one `ToolCallRequest` with valid JSON), `test_sends_think_flag` (inspect request body), `test_http_401_raises_provider_error`, `test_stream_connection_error_raises_provider_error` (transport raises `httpx.ConnectError` → `ProviderError` whose `user_message` says the chat model isn't reachable).
- [ ] **Step 2: Run** `uv run pytest tests/test_chat_provider.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `chat.py`.**
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): streaming OpenAI-compatible chat client"`

### Task 9: Tools framework, agent loop and turn runner

**Files:**
- Create: `server/src/hearthkeep/tools/base.py`, `tools/demo.py`, `agent/loop.py`, `agent/runner.py`
- Test: `server/tests/test_agent_loop.py`, `server/tests/test_runner.py`

**Interfaces:**
- Consumes: `OpenAIChatClient` (Task 8), `decide`/`get_level`/`effective_level` (Task 5), `ApprovalStore` (Task 6), `audit.record`.
- Produces: `@dataclass class ToolResult: ok: bool; content: str; data: dict | None = None`; `@dataclass class ToolContext: chat_id: str; turn_id: str; settings: Settings; conn: sqlite3.Connection`; `class Tool(Protocol): name: str; level_key: str (the permission key, e.g. "github"; several tools can share one); description: str; parameters: dict (JSON Schema); def classify(self, args: dict) -> ActionClass; def summarize(self, args: dict) -> str; async def run(self, args: dict, ctx: ToolContext) -> ToolResult`; `class ToolRegistry: register(tool); available(conn, docker_available) -> list[Tool]` (omits tools whose effective level is OFF); `schemas(tools) -> list[dict]` (OpenAI tool format).
- Produces: `tools/demo.py`: `ClockTool` (name `clock`, Look, returns current local time) and `NoteTool` (name `note`, Change local, appends a line to `<data>/workspace/output/notes.txt`). Both registered under tool-level key `demo` (default ALLOWED) so Phase 1 exercises RUN and ASK paths end to end; Phase 2+ replaces them with real tools.
- Produces: agent events (dicts with `type`): `text_delta{text}`, `reasoning_delta{text}`, `step_started{step_id, tool, summary}`, `step_finished{step_id, ok, preview}`, `approval_required{approval_id, tool, summary, detail, requires_step_up}`, `approval_resolved{approval_id, status}`, `turn_done{}`, `turn_error{message}`.
- Produces: `class AgentLoop: __init__(client, registry, approvals: ApprovalStore, conn, settings); async run_turn(chat_id: str, turn_id: str, history: list[dict], think: bool) -> AsyncIterator[dict]`. Each model tool call: unknown tool or invalid JSON or schema failure → tool result `{"error": ...}` back to the model; otherwise `decide(...)`: RUN → run; ASK/ASK_STEP_UP → create approval, emit `approval_required`, `await approvals.wait`, then run on approve or return "The owner declined this action" / "The request expired" to the model; DENY → error result "This tool is turned off". Approving a CHANGE_LOCAL request under ALLOWED inserts a `chat_grants` row. Stops after `settings.max_agent_steps` tool rounds with a final message saying it hit the step limit.
- Produces: `class TurnRunner: start(chat_id, user_text, think) -> str (turn_id)`; `subscribe(chat_id, turn_id, after_seq: int = 0) -> AsyncIterator[dict]` (replays persisted `turn_events` then live ones); turns run as `asyncio` tasks owned by the runner, so they continue when subscribers disconnect; events are persisted to `turn_events`; final assistant text and tool messages are persisted to `messages`.

- [ ] **Step 1: Write the failing tests** with a `FakeChatClient` scripted per call:
  - `test_plain_reply_streams_text_then_done`
  - `test_allowed_look_tool_runs_without_approval`
  - `test_change_local_asks_once_then_granted_for_chat` (first call → approval; approve; second call same chat → runs directly)
  - `test_outward_requires_step_up_approval` (a test tool classified OUTWARD; approval has `requires_step_up=True`)
  - `test_declined_approval_reported_to_model`
  - `test_off_tool_not_offered_and_call_denied`
  - `test_malformed_tool_call_returns_error_to_model` (invalid JSON, unknown tool name, missing required arg → each produces an error tool message and the loop continues; each counts as a step)
  - `test_step_limit_stops_loop`
  - `test_provider_failure_ends_turn_with_error_event` (client raises `ProviderError` mid-stream → `turn_error` with `user_message`; a second turn on the same chat succeeds)
  - `test_secrets_not_in_model_messages` (tool returning a value equal to a stored secret is redacted before reaching the model)
  - Runner: `test_turn_survives_subscriber_disconnect` (subscribe, cancel subscriber after first event, wait → turn completes and all events are persisted; a new subscriber with `after_seq=0` replays everything), `test_subscribe_after_seq_skips_seen_events`.
- [ ] **Step 2: Run** `uv run pytest tests/test_agent_loop.py tests/test_runner.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `tools/base.py`, `tools/demo.py`, `agent/loop.py`, `agent/runner.py`. Use `jsonschema` for argument validation. Secret redaction: replace any substring equal to a value in `SecretStore` with `[redacted]` in tool results.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): agent loop with permission-gated tools and background turns"`

### Task 10: HTTP API, CLI and smoke test

**Files:**
- Create: `server/src/hearthkeep/api/app.py`, `api/routes_auth.py`, `api/routes_chats.py`, `api/routes_approvals.py`, `api/routes_settings.py`, `api/routes_audit.py`, `server/src/hearthkeep/cli.py`, `scripts/smoke_e2e.py`
- Test: `server/tests/test_api.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `create_app(settings: Settings, *, verifier: Verifier, chat_client, registry: ToolRegistry, clock=utcnow) -> FastAPI`. Session cookie `hk_session`: HttpOnly, SameSite=Strict, Secure unless origin is localhost. CSRF: state-changing requests require header `X-Hearthkeep: 1` and an `Origin` equal to `public_origin`.
- Routes (all JSON; all except enroll/login/health need a valid session):
  - `GET /api/health` → `{status, chat_model_reachable}`
  - `POST /api/auth/enroll/options {code}` → registration options (code consumed only on successful verify); `POST /api/auth/enroll/verify {code, response, device_name}` → sets session
  - `POST /api/auth/login/options` (409 `{reason:"no_devices"}` when nothing is enrolled yet) / `POST /api/auth/login/verify {response}` → sets session; `POST /api/auth/logout`
  - `POST /api/auth/stepup/options {purpose, approval_id?}` / `POST /api/auth/stepup/verify {response, purpose, approval_id?}` → `{step_up_token}`
  - `POST /api/devices/add-code` (requires step-up token, purpose `add_device`) → `{code}`; `GET /api/devices`; `DELETE /api/devices/{id}`
  - `GET /api/chats`, `POST /api/chats {think}`, `GET /api/chats/{id}/messages`, `POST /api/chats/{id}/messages {text}` → `{turn_id}`, `GET /api/chats/{id}/turns/{turn_id}/events?after=` (Server-Sent Events)
  - `GET /api/approvals?status=pending`, `POST /api/approvals/{id} {approve: bool, step_up_token?}` → 409 `{reason}` on `already_resolved|expired`, 403 `{reason:"step_up_required"}`
  - `GET /api/settings/tools` → `[{tool, level, effective_level, description}]`; `PUT /api/settings/tools/{tool} {level, step_up_token?}` → 403 when widening without a valid step-up token (purpose `settings`)
  - `GET /api/audit?limit=&q=`
  - Static: serves `web/dist` at `/` with SPA fallback.
- Produces: CLI `hearthkeep serve --data PATH` (inits the data folder, validates settings, prints a first-device enrollment code when no credentials exist, starts uvicorn on `settings.host:settings.port`); `hearthkeep enroll-code --data PATH` (issues a fresh first-device code, only if none enrolled). `serve --insecure-dev-auth` swaps in an auto-accepting verifier for development and the smoke test; it is refused unless `public_origin` is a localhost origin, and prints a loud warning.

- [ ] **Step 1: Write the failing tests** with FastAPI's test client, `FakeVerifier` and `FakeChatClient`: `test_unauthenticated_requests_401`, `test_enroll_first_device_then_code_reuse_rejected`, `test_first_device_enroll_blocked_after_enrolment`, `test_post_message_streams_events_over_sse`, `test_outward_approval_without_step_up_403`, `test_outward_approval_with_step_up_runs_tool`, `test_double_resolve_returns_409`, `test_widen_tool_level_requires_step_up`, `test_narrow_tool_level_without_step_up_ok`, `test_state_change_without_csrf_header_rejected`, `test_settings_changes_audited`, `test_insecure_dev_auth_refused_for_non_localhost_origin`, `test_login_options_409_when_no_devices`.
- [ ] **Step 2: Run** `uv run pytest tests/test_api.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the app, routes and CLI.
- [ ] **Step 4: Run the full suite** `uv run pytest -v && uv run ruff check`. Expected: all PASS, no lint errors.
- [ ] **Step 5: Write `scripts/smoke_e2e.py`**: against a running Hearthkeep (dev mode, `FakeVerifier` enabled only by `--insecure-dev-auth` on localhost) and the real chat model, asks "What time is it?" and checks that the `clock` tool ran and the reply contains the time; then asks "Add a note saying hello" and checks an approval appears.
- [ ] **Step 6: Run the smoke test** with the local chat server running. Expected: both checks print `OK`.
- [ ] **Step 7: Commit.** `git commit -m "feat(server): HTTP API, CLI and e2e smoke script"`

### Task 11: Web app shell, sign-in and design system

**Files:**
- Create: `web/package.json`, `vite.config.ts` (dev proxy `/api` → `127.0.0.1:8765`), `tsconfig.json`, `index.html`, `public/manifest.webmanifest`, `public/icon-192.png`, `public/icon-512.png`, `src/styles/tokens.css`, `src/styles/base.css`, `src/api/client.ts`, `src/api/webauthn.ts`, `src/shell/AppShell.tsx`, `src/shell/Sidebar.tsx`, `src/shell/TopBar.tsx`, `src/views/SignIn.tsx`, `src/views/Placeholder.tsx`, `src/App.tsx`, `src/main.tsx`
- Test: `web/src/__tests__/shell.test.tsx`, `web/src/__tests__/signin.test.tsx`

**Interfaces:**
- Consumes: the API routes from Task 10.
- Produces: `api/client.ts`: `api<T>(path: string, init?: RequestInit): Promise<T>` (adds `X-Hearthkeep: 1`, `credentials: "same-origin"`, throws `ApiError{status, reason}`); `api/webauthn.ts`: `enroll(code: string, deviceName: string)`, `login()`, `stepUp(purpose: "approval" | "settings" | "add_device", approvalId?: string): Promise<string>` using `@simplewebauthn/browser`.
- Produces: `AppShell` with sidebar items Today, Chat, Images, Analysis, Repos, Files, and Tools & permissions at the bottom; views not built in Phase 1 render `Placeholder` ("Arrives in a later update"). Top bar shows the view title and a status pill fed by `/api/health` (green dot + model name when reachable, amber "Chat model offline" otherwise). Below 560 px the sidebar becomes bottom tabs (Today, Chat, Images, Files, More).

- [ ] **Step 1: Scaffold** with `npm create vite@latest web -- --template react-ts`, add the dependencies from the Tech Stack, `vitest` + `@testing-library/react` + `jsdom`.
- [ ] **Step 2: Write the failing tests**: `renders sidebar with seven destinations`, `status pill shows offline when health says unreachable`, `sign-in shows enrollment form when server says no devices` (mock `/api/health` + a `needs_enrollment` flag returned by `POST /api/auth/login/options` as 409 `{reason:"no_devices"}`), `sign-in calls login() and routes to Chat on success`.
- [ ] **Step 3: Run** `cd web && npm test`. Expected: FAIL.
- [ ] **Step 4: Implement** the tokens (Global Constraints colours, Geist fonts via `@fontsource`), base styles (true black page, 15 px body, `-webkit-font-smoothing`, focus rings in accent, reduced-motion handling), the shell, sign-in (one large "Sign in with passkey" button; enrollment form with code + device name), PWA manifest (`display: standalone`, `background_color` and `theme_color` `#000000`, name "Hearthkeep").
- [ ] **Step 5: Run** `npm test && npm run build`. Expected: PASS; `web/dist` produced.
- [ ] **Step 6: Commit.** `git commit -m "feat(web): dark shell, passkey sign-in, PWA manifest"`

### Task 12: Chat view, approval cards and Tools & permissions

**Files:**
- Create: `web/src/api/events.ts`, `src/shell/Composer.tsx`, `src/views/Chat.tsx`, `src/views/ToolsPermissions.tsx`, `src/components/ApprovalCard.tsx`, `src/components/WorkedSteps.tsx`, `src/components/TriToggle.tsx`
- Test: `web/src/__tests__/chat.test.tsx`, `web/src/__tests__/approval.test.tsx`, `web/src/__tests__/permissions.test.tsx`

**Interfaces:**
- Consumes: Task 10 routes and event types from Task 9; `api`, `stepUp` from Task 11.
- Produces: `subscribeTurn(chatId: string, turnId: string, onEvent: (e: AgentEvent) => void, afterSeq?: number): () => void` (EventSource; reconnects with the last seen `seq` after a drop).
- Chat view: centred 760 px thread; owner bubbles right on `#1f1f22`; assistant text plain; reasoning hidden behind "Thinking" disclosure; tool steps folded in `WorkedSteps` ("Worked for N seconds · K steps"); `ApprovalCard` inline for `approval_required` events (amber border, title from `summary`, detail rendered as a monospace block, buttons **Approve** (primary), **Decline** (ghost); when `requires_step_up` the Approve button first calls `stepUp("approval", id)` and its note reads "Approving asks for Face ID / Touch ID"); floating composer with **Think** toggle and model name; streaming text appears token by token; chat list in a slim column on wide screens.
- Tools & permissions view: one row per tool from `/api/settings/tools` with description and a `TriToggle` (Allowed / Ask / Off); widening calls `stepUp("settings")` before `PUT`; shows "Needs Docker" note when `effective_level` differs from `level`.

- [ ] **Step 1: Write the failing tests**: `streams text deltas into the assistant message`, `folds tool steps and shows count`, `approval card approve without step-up posts approve:true`, `approval card with requires_step_up calls stepUp before posting and sends token`, `approval card shows "Already handled" on 409`, `widening a tool level asks for step-up; narrowing does not`, `reconnects event stream after drop without duplicating text`.
- [ ] **Step 2: Run** `npm test`. Expected: FAIL.
- [ ] **Step 3: Implement** the components and views. Motion: views fade/rise in 0.45 s `cubic-bezier(.2,.8,.2,1)`, disabled under reduced motion.
- [ ] **Step 4: Run** `npm test && npm run build`. Expected: PASS.
- [ ] **Step 5: Manual check** in the browser at `http://localhost:8765` (dev auth on localhost only): sign in, ask the time (clock tool runs), ask to add a note (approval card appears, approve, note written), turn the demo tool Off (it disappears from the model's options), close and reopen the tab mid-reply (reply completes).
- [ ] **Step 6: Commit.** `git commit -m "feat(web): chat with streaming, approval cards, tools and permissions"`

### Task 13: Phase 1 docs and public-safety check

**Files:**
- Create: `docs/security.md`
- Modify: `README.md`
- Test: whole-repo privacy scan

- [ ] **Step 1: Write `docs/security.md`**: network model (127.0.0.1 + Tailscale only), passkeys and step-up, the action-class × level table from `permissions.py`, what the model can never do, data folder layout, how to report issues.
- [ ] **Step 2: Update `README.md`** with a "Run the Phase 1 core" section: prerequisites (Python 3.12+, uv, Node 20+, a running OpenAI-compatible chat server), `uv sync`, `npm ci && npm run build`, `uv run hearthkeep serve --data <data>`, first-device enrollment with the printed code. Use placeholders only.
- [ ] **Step 3: Run** `python scripts/privacy_scan.py $(git ls-files)` and `git log -p | python scripts/privacy_scan.py -` (history scan). Expected: no findings.
- [ ] **Step 4: Commit.** `git commit -m "docs: security model and phase 1 quick start"`
- [ ] **Step 5: Stop for the owner.** Creating the public GitHub repository and pushing is an outward action: show the owner the file list and scan result and push only after their explicit go-ahead.
