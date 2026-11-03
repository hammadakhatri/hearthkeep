# Hearthkeep Phase 5, Project 1: Add-on Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One server-side `Addons` registry knows the state of the chat model and each add-on (Analysis, Images, GitHub, Phone and devices, Start at login), and every tool, screen and message uses it, so nothing is offered or shown that can't work, and nothing tells the owner to type a command.

**Architecture:** `hearthkeep/addons.py` holds the registry. It reuses today's detection (the sandbox handle's Docker and image status, `GitRunner.available`, the saved GitHub token, `startup.cached_status`, the model cards and their files on disk, the GPU scheduler, the chat health probe) plus a new Tailscale probe. Slow checks run in threads at start-up and every 30 s in the background. The chat and GitHub states are worked out live from cheap, already-cached facts. `GET /api/addons` serves the cache. `ToolRegistry` asks the registry before offering a tool, and the agent loop's call-time denial stays. When there's no chat model and no chat server in `config.toml`, the server still starts in a new `none` chat mode. The web app reads `/api/addons` once, in the shell, and gates the composer, the top bar, Images, Analysis, Repos and Tools & permissions from it. This project adds no install or start actions: buttons lead to Settings › Add-ons, which explains each add-on and can check again.

**Tech Stack:** The Phase 1–4 stack (Python 3.12.6/uv, FastAPI, httpx, pytest, ruff; React 19 + TypeScript, Vitest). The Tailscale CLI is used if it's installed. No new runtime dependency.

**Spec:** `docs/superpowers/specs/2026-11-03-hearthkeep-phase-5-design.md` §4 (this project), with §1–3, §6a and §12 for context; audit evidence in `.superpowers/phase5-audit-notes.md` (UX items U3, U4, U5, U6, U7, U9 and U22 are fixed here; U8's GitHub notes in part). The earlier plans' Global Constraints still apply (`2026-10-26-hearthkeep-foundation.md`, `-models-images.md`, `-files-analysis.md`, `2026-11-03-hearthkeep-github.md`).

## Global Constraints

- **Loopback only.** Nothing new listens anywhere. `tailscale status --json` is only read, never `tailscale serve`.
- **A session on every route.** `GET /api/addons` and `POST /api/addons/refresh` use `current_session`. The refresh is a POST, so the existing CSRF checks apply, and it's rate-limited with `rate_limit(request, "addons")`. `/api/health` stays open, as today.
- **No secrets, repo names or personal paths in the audit log or server logs.** Add-on JSON never carries a file path, a token or the tailnet name. Probe failures log the exception type only.
- **No AI attribution or co-author lines in commit messages.** Every commit uses a Conventional Commits message with no trailers.
- **Existing tests keep passing.** A test that pins behaviour or copy this project deliberately changes is updated in the task that changes it, and the task names it. Behaviour changes come with tests.
- **Copy rules.** Use "passkey", never "Face ID / Touch ID". No CLI command (`hearthkeep …`, `uv run …`, `--data`) and no placeholder (`<data>`, `<home>`) in any owner-visible or model-visible message. A message that sends the owner somewhere names a screen: "Open Settings › Add-ons". No new "Arrives in a later update" or "see docs/…".
- **Add-on ids and states (exact strings).** `chat`: `not_set_up`, `ready`, `starting`, `offline`, `error`. `analysis`: `not_set_up`, `docker_stopped`, `needs_image`, `ready`. `images`: `not_set_up`, `error`, `ready`. `github`: `not_set_up`, `needs_git`, `needs_token`, `ready`. `devices`: `this_pc_only`, `needs_tailscale`, `ready`. `startup`: `off`, `on`, `unsupported`. `ready` is true only for `ready` and `on`.
- **Fix action ids (exact strings):** `install`, `start_docker`, `build_image`, `add_token`, `open_setup`, `turn_on`. In this project only `add_token` (the GitHub panel) and `turn_on` (the Start at login panel) lead somewhere that acts. Every other action leads to the add-on's card in Settings › Add-ons.
- **Upgrade safety (§6a).** Today's formats are read as they are: a hand-written `models.toml`, cards with `-ngl 999`, an external `chat_base_url`, and stored tool levels including `wallet`, `news` and `demo`. Stored levels are never deleted in this project (the §6a migration belongs to project 2).
- **Commands.** Server, from `server/`: `uv run pytest -q` and `uv run ruff check . ../scripts`. Web, from `web/`: `npx vitest run src/__tests__/<file>`, `npm test` and `npm run build`.

## Review Focus

1. **Docker stops after start-up.** Within one background refresh (or at once on "Check again"), `analysis` goes from `ready` to `docker_stopped`. `python`, `read_pages`, `files` and `repo_test` stop being offered, and a call that still arrives is denied with the not-ready message, never run. (Task 1: `test_docker_stopped_after_startup_turns_analysis_off`; Task 4: `test_python_not_offered_after_docker_stops`.)
2. **A model card whose files were deleted.** The chat model's GGUF, or every image card's files, removed while Hearthkeep runs: the next refresh reports chat `error` ("can't find the chat model's files") or images `error`. Nothing crashes, and the add-on JSON names no path. (Task 1: `test_deleted_card_files_report_error_without_paths`.)
3. **An external chat server that's down vs one never configured.** `chat_base_url` set in `config.toml` with nothing answering is `offline`: the composer is disabled with visible text, and messages still post if the owner insists through the API. No `chat_base_url` and no owned model is `not_set_up`: the server starts, `/api/health` says `not_set_up` without probing, and `POST …/messages` answers 409 `chat_not_set_up`. (Task 1: `test_external_down_is_offline_never_configured_is_not_set_up`; Task 3: `test_no_chat_model_server_starts_and_refuses_messages`.)
4. **Stored levels for removed tools.** A database with `wallet`, `news` and `demo` rows: Tools & permissions doesn't list them, `PUT /api/settings/tools/wallet` is 404, turns run normally, and the rows are still there. (Task 4: `test_stored_levels_for_removed_tools_are_harmless`.)
5. **The token is removed while tools are offered.** The GitHub tools were offered at the start of a model round, and the token is deleted before the call is handled: the call is denied with the not-ready message, the tool never runs, and `tool.denied` is audited. (Task 4: `test_token_removed_mid_round_denies_github_call`.)

## Calls made in this plan

The spec doesn't settle these. Check them while reading:

- **"Explicitly configured external server"** means `chat_base_url` is present in `config.toml`. The example config now ships it commented out, so fresh data folders start `not_set_up`. Every existing `config.toml` was copied from the old example and has the key, so existing installs stay external.
- **A hand-written owned setup** (exe, card and `model_dirs` set) whose files are missing is chat `error` ("can't find the chat model's files"), not `not_set_up`. The owner did set it up, and the honest message is that the files are gone. The server still starts.
- **An owned chat model that isn't loaded yet** is `ready` with `details.loaded = false`. `starting` is used only while llama-server is actually loading. §4.1 has no standby state.
- **Views keep saved work under the set-up panel.** The Images gallery, Analysis charts and the Repos list (without Update) stay visible. Only forms and actions that can't work are replaced.
- **`repo_install`'s add-on is `github` only.** Its Docker need stays a call-time message, now without a command. `repo_test` needs `github` and `analysis`, as the brief says. `analysis` means Docker plus the analysis sandbox image; the repo image's absence keeps its own message.
- **An https `public_origin` that isn't `*.ts.net`** counts as devices `ready`, because Hearthkeep can't check another front end.
- **Fix actions with no in-app action yet** (`install`, `start_docker`, `build_image`, `open_setup`) lead to the add-on's card in Settings › Add-ons. There the owner sees the explanation and "Check again". The buttons that act arrive with project 2.
- **The Start at login panel still shows its CLI command** until project 2's on/off switch replaces it (U11).
- **Demo tools move into the tests.** The `demo` default level exists only in the tests' conftest.

---

## File structure

```
server/src/hearthkeep/
  addons.py            # NEW: Addons registry, AddonStatus, copy table, probes, Tailscale, ModelSetup
  procutil.py          # NEW: find_executable, NO_WINDOW (project 4 grows it)
  api/routes_addons.py # NEW: GET /api/addons, POST /api/addons/refresh
  config.py            # Settings.chat_base_url_set
  config.example.toml  # chat_base_url commented out (and the repo-root copy)
  runtime/gpu.py       # uses find_executable; GpuScheduler.loading()
  runtime/stack.py     # ModelStack.chat_mode, ModelStack.runtime
  models/runtime.py    # IMAGE_FILE_FOLDERS, card_file_paths
  cli.py               # chat mode none; demo tools gone; console copy
  providers/chat.py    # NoChatModelClient, copy
  tools/base.py        # ToolRegistry.addons gate, ToolRegistry.is_available
  agent/loop.py        # registry.is_available, NOT_READY
  permissions.py       # wallet, news, demo removed from DEFAULT_LEVELS
  api/app.py, api/common.py, api/routes_settings.py, api/routes_chats.py
  (copy only) tools/python.py, sandbox/runner.py, github/deps.py,
              providers/scheduled_chat.py, runtime/supervisor.py
  tools/demo.py        # MOVED to server/tests/demo_tools.py
web/src/
  api/addons.ts                  # NEW: types, getAddons, refreshAddons, useAddons
  shell/AddonsContext.tsx        # NEW: provider, useAddon, useOpenSettings, actionTarget
  components/AddonsPanel.tsx     # NEW: Settings › Add-ons
  components/SetUpPanel.tsx      # NEW: the one "set up" panel for views
  styles/addons.css              # NEW
  shell/AppShell.tsx, shell/TopBar.tsx, shell/Composer.tsx, views/Chat.tsx,
  views/Settings.tsx, views/Images.tsx, views/Analysis.tsx, views/Repos.tsx,
  views/ToolsPermissions.tsx, components/GitHubPanel.tsx, components/FoldersPanel.tsx,
  components/ModelList.tsx, test/fetch.ts, test/addons.tsx (NEW)
```

---

### Task 1: The `Addons` registry and its detection

**Files:**
- Create: `server/src/hearthkeep/addons.py`, `server/src/hearthkeep/procutil.py`
- Modify: `server/src/hearthkeep/runtime/gpu.py` (`_nvidia_smi_path` uses `find_executable`; add `GpuScheduler.loading()`), `server/src/hearthkeep/runtime/stack.py` (fields), `server/src/hearthkeep/models/runtime.py` (file paths), `server/tests/conftest.py` (hermetic probes)
- Test: `server/tests/test_addons.py`, existing `server/tests/test_gpu.py`

**Interfaces:**
- Produces (`procutil.py`): `NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)`; `find_executable(name: str, fallbacks: Sequence[Path] = ()) -> Path | None`. This is `_nvidia_smi_path`'s rule made general: `shutil.which(name)` only if absolute and not in the current folder, else the first fallback that is an absolute existing file. `gpu._nvidia_smi_path()` becomes `find_executable("nvidia-smi", [...])` with today's two fallbacks.
- Produces (`gpu.py`): `GpuScheduler.loading(self) -> str | None` returns the id being loaded (`self._loading`).
- Produces (`stack.py`): `ModelStack.chat_mode: Literal["owned", "external", "none"] = "external"` and `ModelStack.runtime: RuntimeConfig | None = None`. The default stack stays external, so test stacks keep today's meaning.
- Produces (`models/runtime.py`): `IMAGE_FILE_FOLDERS = {"unet": "diffusion_models", "text_encoder": "text_encoders", "vae": "vae"}`; `card_file_paths(card: ModelCard, rt: RuntimeConfig) -> tuple[Path, ...] | None`. A chat card gives `rt.model_dirs[card.id] / name` for each file. An image card gives `rt.comfyui_model_root / IMAGE_FILE_FOLDERS[key] / name`. It returns None when that folder isn't configured.
- Produces (`addons.py`):
  - `AddonId = Literal["chat", "analysis", "images", "github", "devices", "startup"]`; `ADDON_IDS: tuple[AddonId, ...]` in that order; `REFRESH_SECONDS = 30.0`.
  - `TOOL_ADDONS: dict[str, tuple[AddonId, ...]] = {"python": ("analysis",), "read_pages": ("analysis",), "files": ("analysis",), "generate_image": ("images",), "repo_read": ("github",), "repo_edit": ("github",), "repo_install": ("github",), "github": ("github",), "repo_test": ("github", "analysis")}`.
  - `LEVEL_ADDONS: dict[str, tuple[AddonId, ...]] = {"python": ("analysis",), "files": ("analysis",), "images": ("images",), "github": ("github",), "github_tests": ("github", "analysis")}`.
  - `@dataclass(frozen=True) AddonStatus(id: AddonId, state: str, message: str, action: str | None, short: str | None, details: dict = {})`, with property `ready -> bool` and `to_json() -> dict`. The JSON is `{id, name, about, requires, state, ready, message, action, short, details}`, where `name`, `about` and `requires` come from `ABOUT`.
  - `@dataclass(frozen=True) TailscaleState(installed: bool, running: bool, dns_name: str | None)`; `tailscale_state(run=subprocess.run) -> TailscaleState`. It finds `tailscale` with `find_executable("tailscale", [<ProgramFiles>/Tailscale/tailscale.exe])` and runs `[exe, "status", "--json"]` with timeout 5 and `creationflags=NO_WINDOW`. It reads `BackendState == "Running"` and `Self.DNSName`, without its trailing dot and lowercased. Any failure gives `installed=True, running=False`. With no executable it's `TailscaleState(False, False, None)`.
  - `@dataclass(frozen=True) ModelSetup(chat_mode: Literal["owned","external","none"], chat_model: str, chat_paths: tuple[Path, ...] = (), images_managed: bool = False, comfy_paths: tuple[Path, ...] = (), image_cards: dict[str, tuple[Path, ...]] = {})`.
  - `model_setup(stack: ModelStack, settings: Settings) -> ModelSetup`. `chat_paths` = `rt.llama_server_exe` plus `card_file_paths` of the owned chat card. `comfy_paths` = `comfyui_python` and `comfyui_main`. `image_cards` covers each managed image card. With `stack.runtime is None` every path tuple is empty, so there is nothing to check.
  - Module functions: `docker_installed() -> bool` (`find_executable("docker", [<ProgramFiles>/Docker/Docker/resources/bin/docker.exe]) is not None`) and `startup_status() -> StartupStatus` (`startup.cached_status(time.monotonic())`).
  - `@dataclass Probes(sandbox: Callable[[], Awaitable[SandboxStatus | None]], docker_configured_off: bool, git_available: Callable[[], bool], token_saved: Callable[[], bool], chat_reachable: Callable[[], Awaitable[bool]], docker_installed: Callable[[], bool] | None = None, tailscale: Callable[[], TailscaleState] | None = None, startup: Callable[[], StartupStatus] | None = None, file_exists: Callable[[Path], bool] = Path.is_file)`. A None probe means the module function of that name, looked up when it's called, so a test's monkeypatch of the module function takes effect.
  - `class Addons`:
    - `__init__(settings: Settings, setup: ModelSetup, probes: Probes, gpu: GpuScheduler | None = None, clock: Callable[[], datetime] = utcnow)`.
    - `async refresh(*ids: AddonId) -> list[AddonStatus]`: with no ids, all of them. Concurrent calls share one `asyncio.Lock`. Every blocking probe (file checks, `docker_installed`, `tailscale`, `startup`) runs in `asyncio.to_thread`. `sandbox()` and `chat_reachable()` are awaited (they already work off the loop). A probe that raises gives the add-on's not-ready state and a warning log with the type only.
    - `async run_forever(interval: float = REFRESH_SECONDS) -> None`: refreshes until cancelled and never raises.
    - `get(addon_id) -> AddonStatus`: `chat` and `github` are worked out at call time, from the cached file and reachability results plus `gpu.loading()` and `gpu.resident()`, or plus `git_available()` and `token_saved()`. The other add-ons come from the cache.
    - `all() -> list[AddonStatus]` (in `ADDON_IDS` order); `ready(*ids) -> bool`; `tool_ready(tool_name: str) -> bool` (`TOOL_ADDONS`; a tool not listed is True); `level_addon(level_key: str) -> AddonStatus | None` (the first not-ready of `LEVEL_ADDONS[key]`, else the first of them, else None); `checked_at: datetime | None`.
- How each state is decided:
  - **chat:** mode `none` gives `not_set_up`. Mode `owned`: a missing file in `chat_paths` gives `error`; `gpu.loading() == chat_model` gives `starting`; otherwise `ready` with `details={"model": chat_model, "loaded": chat_model in gpu.resident()}`. Mode `external`: the last `chat_reachable()` result gives `ready` (`details={"model": chat_model, "loaded": True}`) or `offline`.
  - **analysis:** `docker_configured_off` gives `not_set_up`. Otherwise `sandbox()` is called: None, or a status with `docker` False, gives `docker_stopped` if `docker_installed()`, else `not_set_up`; `image` False gives `needs_image`; otherwise `ready`.
  - **images:** not `images_managed` gives `not_set_up`. A missing comfy path, or no image card with all its files present, gives `error`. Otherwise `ready`.
  - **github:** neither git nor a token gives `not_set_up`; no git gives `needs_git`; no token gives `needs_token`; otherwise `ready`.
  - **devices:** a `public_origin` host in `LOOPBACK_HOSTS` gives `this_pc_only`, and Tailscale is never run. An https host ending in `.ts.net`: `running` with `dns_name` equal to the host gives `ready`, anything else `needs_tailscale`. Any other https host gives `ready`, because Hearthkeep can't check it.
  - **startup:** `supported` False gives `unsupported`; enabled and not `startup.on_for_another_folder(...)` gives `on`; otherwise `off`.
- Copy (`ABOUT` and the state table: exact strings):

| id | name | about | requires |
|---|---|---|---|
| chat | Chat model | Chat with a model that runs on this PC. | llama.cpp and a chat model, or a chat server you already run on this PC. |
| analysis | Analysis | Python analysis of your files, and reading PDFs and scans. | Docker Desktop, and the analysis sandbox (built once, about 1 GB). |
| images | Images | Create and edit images on this PC. | ComfyUI and at least one image model. |
| github | GitHub | Work on your GitHub repos: read, edit, test and open pull requests. | Git 2.31 or newer and a GitHub token. Running tests also needs Analysis. |
| devices | Phone and devices | Use Hearthkeep from your phone and other devices. | Tailscale, signed in, with HTTPS turned on. |
| startup | Start at login | Hearthkeep starts on its own when you sign in. | Windows. |

| id.state | message | action | short |
|---|---|---|---|
| chat.not_set_up | Set up your chat model to start chatting. | open_setup | Needs a chat model |
| chat.ready | Ready. (owned and not loaded: "Ready. It loads when you send a message.") | – | – |
| chat.starting | Loading the chat model… | – | – |
| chat.offline | The chat server isn't answering. Make sure it's running on this PC. | open_setup | Chat server offline |
| chat.error | Hearthkeep can't find the chat model's files on this PC. | open_setup | Chat model files missing |
| analysis.not_set_up | Docker Desktop isn't installed on this PC. | install | Needs Docker |
| analysis.docker_stopped | Docker Desktop isn't running. Start it, then check again. | start_docker | Needs Docker |
| analysis.needs_image | The analysis sandbox isn't built yet. | build_image | Needs the analysis sandbox |
| analysis.ready | Ready. | – | – |
| images.not_set_up | ComfyUI and an image model aren't set up on this PC. | install | Needs Images |
| images.error | Hearthkeep can't find ComfyUI or the image model files on this PC. | open_setup | Needs Images |
| images.ready | Ready. | – | – |
| github.not_set_up | Git isn't installed and no GitHub token is saved. | install | Needs Git and a token |
| github.needs_git | Git 2.31 or newer isn't installed on this PC. | install | Needs Git |
| github.needs_token | Save a GitHub token to let Hearthkeep work on your repos. | add_token | Needs a GitHub token |
| github.ready | Ready. | – | – |
| devices.this_pc_only | Hearthkeep only answers on this PC. | open_setup | – |
| devices.needs_tailscale | Remote access is on, but Tailscale isn't running or signed in on this PC. | open_setup | – |
| devices.ready | Your phone and other devices can reach Hearthkeep. | – | – |
| startup.off | Hearthkeep doesn't start on its own when you sign in. (on for another folder: "Start at login is on for another data folder.") | turn_on | – |
| startup.on | Hearthkeep starts when you sign in to Windows. | – | – |
| startup.unsupported | Start at login is built in only on Windows. | – | – |

- `conftest.py` gets an autouse fixture `hermetic_addon_probes`. It monkeypatches `hearthkeep.addons.tailscale_state` to return `TailscaleState(False, False, None)` and `hearthkeep.addons.startup_status` to return `StartupStatus(False, False, None)`, so no test runs `tailscale` or `schtasks` through the registry. Tests of those two functions call them with their own `run` fake.

- [ ] **Step 1: Write the failing tests**

```python
# test_addons.py. `probes(**over)` builds Probes from fakes (all ready by default); `owned_setup(tmp_path)` writes the files.
async def test_every_state_has_copy_without_commands_or_placeholders():
    for status in all_states_from_table():            # one AddonStatus per row of the table
        for text in (status.message, status.short or "", *ABOUT[status.id]):
            assert "hearthkeep " not in text.lower() and "<" not in text and "--" not in text

async def test_docker_stopped_after_startup_turns_analysis_off():
    sandbox = FakeSandboxStatus(docker=True, image=True)
    addons = Addons(settings, setup, probes(sandbox=sandbox.call))
    await addons.refresh()
    assert addons.get("analysis").state == "ready" and addons.tool_ready("python")
    sandbox.docker = False
    await addons.refresh("analysis")
    s = addons.get("analysis")
    assert (s.state, s.action) == ("docker_stopped", "start_docker")
    assert not addons.tool_ready("python") and not addons.tool_ready("repo_test")
    assert addons.tool_ready("send_to_devices")       # not in TOOL_ADDONS

async def test_docker_not_installed_is_not_set_up():   # sandbox() None, docker_installed False
async def test_docker_configured_off_is_not_set_up():
async def test_missing_image_is_needs_image():          # action build_image, short "Needs the analysis sandbox"

async def test_deleted_card_files_report_error_without_paths(tmp_path):
    setup = owned_setup(tmp_path)                        # exe + gemma files exist
    addons = Addons(settings, setup, probes(), gpu=fake_gpu())
    await addons.refresh()
    assert addons.get("chat").state == "ready"
    (tmp_path / "gemma" / "gemma-4-12b-it-Q6_K.gguf").unlink()
    await addons.refresh("chat")
    chat = addons.get("chat")
    assert chat.state == "error"
    assert str(tmp_path) not in json.dumps(chat.to_json())
    # images: a managed setup whose only image card's vae file is deleted after a "ready" refresh
    assert addons.get("images").state == "error"

async def test_external_down_is_offline_never_configured_is_not_set_up():
    down = Addons(settings, ModelSetup("external", "gemma-4-12b"), probes(chat_reachable=never))
    await down.refresh()
    assert down.get("chat").state == "offline"
    none = Addons(settings, ModelSetup("none", "gemma-4-12b"), probes(chat_reachable=explode))
    await none.refresh()
    assert none.get("chat").state == "not_set_up"     # and chat_reachable was never called

async def test_owned_chat_starting_then_ready_not_loaded():  # gpu.loading() == id → starting; else ready, loaded False
async def test_github_states_follow_git_and_token_live():  # flip token_saved without refresh: get() changes at once
async def test_devices_states(tmp_path):                  # localhost → this_pc_only, tailscale never called; ts.net + running + name → ready; wrong name → needs_tailscale
async def test_startup_states():                         # unsupported / on / off / on for another folder → off with its message
async def test_level_addon_picks_first_not_ready():       # github_tests with github ready, analysis not → analysis
async def test_checks_run_off_the_event_loop():
    seen = []
    def blocking() -> bool:
        seen.append(threading.get_ident())
        return True
    addons = Addons(settings, setup, probes(docker_installed=blocking, file_exists=lambda p: blocking()))
    await addons.refresh()
    assert seen and threading.get_ident() not in seen
async def test_probe_that_raises_gives_not_ready_and_logs_type_only(caplog)
async def test_run_forever_refreshes_and_stops_on_cancel()

def test_tailscale_state_parses_status_json():           # fake run → BackendState Running, DNSName "pc.tail1.ts.net."
def test_tailscale_state_without_binary_or_bad_json()
def test_card_file_paths_for_chat_and_image(tmp_path)    # image vae → <root>/vae/ae.safetensors; unset folder → None
def test_find_executable_ignores_current_folder(tmp_path, monkeypatch)
```

Add to `test_gpu.py`: `test_loading_reports_the_model_being_loaded` (a backend whose load waits on an event).

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_addons.py tests/test_gpu.py -q`
Expected: FAIL (`ModuleNotFoundError: hearthkeep.addons`)

- [ ] **Step 3: Implement `procutil.py`, `GpuScheduler.loading`, the `ModelStack` fields, `card_file_paths` and `addons.py`, then add the conftest fixture**

Keep the copy table as one dict, `_COPY: dict[tuple[AddonId, str], tuple[str, str | None, str | None]]`, so the first test can walk it. The `ModelStack` fields aren't set by anything yet (Tasks 2 and 3 set them).

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check . ../scripts`
Expected: PASS, no lint findings

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(addons): one registry for the chat model and each add-on's state"
```

---

### Task 2: Wire the registry into the app, and `GET /api/addons`

**Files:**
- Create: `server/src/hearthkeep/api/routes_addons.py`
- Modify: `server/src/hearthkeep/api/app.py` (build `Probes` and `Addons` in the lifespan, refresh before `yield`, start and cancel the background task, include the router), `server/src/hearthkeep/api/common.py` (`Services.addons`, `Services.chat_mode`), `server/tests/test_api.py` (the unauthenticated-routes list gains the two routes)
- Test: `server/tests/test_api_addons.py`

**Interfaces:**
- Consumes: Task 1 `Addons`, `Probes`, `model_setup`, `ModelStack.chat_mode`.
- Produces (`common.py`): `Services.addons: Addons | None = None`; `Services.chat_mode: Literal["owned","external","none"] = "external"`.
- Produces (`app.py` lifespan): `Probes`, built from:
  - `sandbox=` an async function returning `(await runner.status())` when `await handle.get()` is a runner, else None;
  - `docker_configured_off=` `settings.docker_available is False` *before* detection replaces it;
  - `git_available=git_runner.available`;
  - `token_saved=lambda: github_token() is not None`;
  - `chat_reachable=check` (the health probe, never called in mode `none`).

  The other probes are left None (the module functions). Then `addons = Addons(settings, model_setup(stack, settings), probes, gpu)`, `Services(..., addons=addons, chat_mode=stack.chat_mode)`, `await addons.refresh()` before `yield`, `registry.addons = addons` (Task 4 reads it), and `task = asyncio.create_task(addons.run_forever())`, cancelled and awaited first in the `finally`. `create_app` gains `addon_probes: Probes | None = None`: a test can pass its own probes, else they're built as above.
- Produces (`routes_addons.py`): `GET /api/addons` → `{"addons": [AddonStatus.to_json(), …], "checked_at": iso8601 | null}`, from the cache only. `POST /api/addons/refresh` → `await hk.addons.refresh()` then the same body (rate group `"addons"`, 429 `rate_limited` past the limit). Both are 503 `unavailable` if `hk.addons is None`. Not audited (read-only).

- [ ] **Step 1: Write the failing tests**

```python
def test_addons_needs_a_session(harness):            # GET and POST → 401; POST without X-Hearthkeep → 403 csrf
def test_addons_lists_all_six_in_order(harness):
    body = signed_in(harness).get("/api/addons").json()
    assert [a["id"] for a in body["addons"]] == ["chat", "analysis", "images", "github", "devices", "startup"]
    chat = body["addons"][0]
    assert set(chat) == {"id", "name", "about", "requires", "state", "ready", "message", "action", "short", "details"}
def test_get_is_served_from_cache(harness):
    probes = CountingProbes()
    client = signed_in(harness, addon_probes=probes)
    before = probes.calls
    for _ in range(5):
        client.get("/api/addons")
    assert probes.calls == before
def test_refresh_reruns_checks_and_is_rate_limited(harness)
def test_harness_sandbox_means_analysis_ready(harness):  # sandbox fake given → "ready"; none → not ready
def test_addons_json_has_no_paths_or_token(harness)       # data dir, token and home folder absent from the body
def test_background_task_cancelled_on_shutdown(harness)   # stop() returns; no "Task was destroyed" warning
```

`harness.start` gains `addon_probes=None`, passed through to `create_app`.

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_api_addons.py -q`
Expected: FAIL (404 for `/api/addons`)

- [ ] **Step 3: Implement the wiring and the routes**

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check . ../scripts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(addons): check add-ons at start-up and in the background, and serve them at /api/addons"
```

---

### Task 3: The "no chat model" state, read safely from today's formats

**Files:**
- Modify:
  - `server/src/hearthkeep/config.py` (`Settings.chat_base_url_set`);
  - `server/src/hearthkeep/config.example.toml` and the repo-root `config.example.toml` (identical: `test_config.py` guards that);
  - `server/src/hearthkeep/cli.py` (`build_model_stack`: mode `none`, `chat_mode` and `runtime` on the stack, console copy);
  - `server/src/hearthkeep/providers/chat.py` (`NoChatModelClient`);
  - `server/src/hearthkeep/api/common.py` (`chat_model_status` for `none`);
  - `server/src/hearthkeep/api/routes_chats.py` (409);
  - `server/tests/test_cli.py` (the `serve` fixture's config gains `chat_base_url`, as every existing install's `config.toml` has it);
  - `docs/install.md` (§4 "Skip this step…", §6 opening line, the troubleshooting line about chat_base_url);
  - `docs/models.md:150`.
- Create: `server/tests/fixtures/upgrade/config.toml` (a verbatim copy of today's `config.example.toml`, `chat_base_url` set), `server/tests/fixtures/upgrade/models.toml` (a hand-written owned file using `{root}` where the test puts its temp folder), `server/tests/fixtures/upgrade/my-gemma.toml` (an owner chat card whose `launch_args` include `"-ngl", "999"`)
- Test: `server/tests/test_no_chat_model.py`, existing `test_cli.py`, `test_config.py`

**Interfaces:**
- Consumes: Task 1 `ModelStack.chat_mode`/`runtime`; Task 2 `Services.chat_mode`, `hk.addons`.
- Produces (`config.py`): `Settings.chat_base_url_set: bool = True`, left out of `_TYPES` like `data_dir`, so `chat_base_url_set = true` in `config.toml` is still "unknown config key". `load_settings` sets it to `"chat_base_url" in raw`. Settings built directly in code keep True, which is today's external meaning.
- Produces (example configs): `chat_base_url` becomes a comment, `# chat_base_url = "http://127.0.0.1:8080/v1"`, under the comment "Set this to use a chat server you already run on this PC. Leave it out to let Hearthkeep run its own chat model." A fresh data folder therefore starts in `not_set_up`. `chat_model` stays set.
- Produces (`cli.build_model_stack`): the owned rule is unchanged (exe set, a chat card, a `model_dirs` entry), so a hand-written `models.toml` behaves as today. Otherwise, with `settings.chat_base_url_set`, it's external as today. Otherwise it's mode `none`:
  - `chat_client = NoChatModelClient()`;
  - the line `Chat: no chat model is set up yet. Open Hearthkeep and go to Settings › Add-ons.`;
  - the two `_warn` texts end "…; Hearthkeep won't run llama-server." instead of naming chat_base_url when the mode is `none`. In every mode they say `models.toml`, not `<data>/models.toml`.

  The stack gets `chat_mode` and `runtime=rt`. The other console lines lose `<data>/`: the images line becomes `Images: not set up. Open Settings › Add-ons to see what it needs.`, and the external line becomes `Chat: using the chat server at chat_base_url (Hearthkeep doesn't start or schedule it).`
- Produces (`providers/chat.py`): `NO_CHAT_MODEL = "No chat model is set up yet. Open Settings › Add-ons to set one up."`; `class NoChatModelClient` with `async stream(messages, tools, think, temperature=None)`, which raises `ProviderError(NO_CHAT_MODEL, "no chat model")` before yielding.
- Produces (`common.py`): with `chat_mode == "none"`, `chat_model_status()` returns `{"chat_model_reachable": False, "chat_model_state": "not_set_up"}` without calling `health_check`.
- Produces (`routes_chats.post_message`): before `runner.start`, if `hk.addons` reports chat `not_set_up`, it raises `ApiError(409, "chat_not_set_up", message=<the chat status message>)`. Nothing is saved.

- [ ] **Step 1: Write the failing tests**

```python
def test_fresh_data_folder_has_no_chat_server(tmp_path):
    init_data_dir(tmp_path)
    assert load_settings(tmp_path).chat_base_url_set is False
def test_todays_config_keeps_its_chat_server(tmp_path):        # fixtures/upgrade/config.toml copied in
    assert load_settings(tmp_path).chat_base_url_set is True
def test_chat_base_url_set_is_not_a_config_key(tmp_path)      # "chat_base_url_set = true" → ConfigError
def test_no_chat_model_server_starts_and_refuses_messages(serve_fresh):
    assert serve_fresh() == 0                                   # config.toml without chat_base_url, no models.toml
    hk = serve_fresh.hk
    assert hk.chat_mode == "none" and hk.addons.get("chat").state == "not_set_up"
    client = serve_fresh.signed_in()
    assert client.get("/api/health").json()["chat_model_state"] == "not_set_up"
    chat = new_chat(client)
    r = client.post(f"/api/chats/{chat}/messages", json={"text": "hi"}, headers=CSRF)
    assert r.status_code == 409 and r.json()["reason"] == "chat_not_set_up"
    assert r.json()["message"] == hk.addons.get("chat").message
    assert hk.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
async def test_no_chat_model_client_raises_provider_error():
    with pytest.raises(ProviderError) as e:
        async for _ in NoChatModelClient().stream([], [], False):
            pass
    assert e.value.user_message == NO_CHAT_MODEL
def test_upgrade_external_config_unchanged(serve, capsys)     # fixtures config → OpenAIChatClient at :8080, chat mode external
def test_upgrade_owned_models_toml_with_ngl_999(serve, tmp_path):
    # fixtures models.toml + my-gemma.toml copied in with {root} filled; files created
    assert serve() == 0
    card, *_ = serve.llama_calls[0]
    assert ("-ngl", "999") in zip(card.launch_args, card.launch_args[1:])
    assert serve.hk.chat_mode == "owned" and serve.hk.addons.get("chat").state == "ready"
def test_owned_config_with_missing_files_starts_with_chat_error(serve, tmp_path)  # exe and GGUF absent → server starts, chat "error"
def test_console_lines_name_no_placeholder(serve, capsys)      # no "<data>" in the startup output
```

`serve_fresh` is the `serve` fixture with `config.toml` = `"docker_available = false\n"` and no `chat_base_url`. `signed_in()` enrols with the fake verifier, as `test_api.signed_in` does.

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_no_chat_model.py tests/test_cli.py tests/test_config.py -q`
Expected: FAIL (`AttributeError: chat_base_url_set`)

- [ ] **Step 3: Implement the settings flag, the example configs, mode `none`, `NoChatModelClient`, the health state, the 409, and update the docs lines**

The docs lines say: with neither `models.toml` nor `chat_base_url`, Hearthkeep starts without a chat model and Settings › Add-ons says so. To use a chat server you already run, set `chat_base_url` in `config.toml`.

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check . ../scripts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server docs
git commit -m "feat(chat): start without a chat model and say so, keeping today's configs working"
```

---

### Task 4: Tools follow their add-on, and Tools & permissions lists only real tools

**Files:**
- Modify:
  - `server/src/hearthkeep/tools/base.py` (registry gate);
  - `server/src/hearthkeep/agent/loop.py` (`registry.is_available`, `NOT_READY`);
  - `server/src/hearthkeep/permissions.py` (`DEFAULT_LEVELS` without `wallet`, `news`, `demo`);
  - `server/src/hearthkeep/cli.py` (`build_registry` registers no demo tools);
  - `server/src/hearthkeep/api/routes_settings.py` (rows and `addon`);
  - `docs/security.md:48` (the demo-tools sentence goes);
  - `docs/install.md:623` (the GitHub troubleshooting line points at Settings › Add-ons).
- Move: `server/src/hearthkeep/tools/demo.py` → `server/tests/demo_tools.py`. Update its importers `test_agent_loop.py`, `test_api.py` and `test_api_models_images.py`.
- Modify tests: `server/tests/conftest.py`:
  - an autouse fixture `demo_level` runs `monkeypatch.setitem(permissions.DEFAULT_LEVELS, "demo", ToolLevel.ALLOWED)`, so the test-only demo key keeps its old default;
  - it's skipped for tests marked `real_levels` (marker registered in `pytest_configure`); harness tests that call `python` with no sandbox and expected `NO_DOCKER` from the tool now expect the loop's `NOT_READY` (find them with `grep -n NO_DOCKER tests/test_api*.py tests/test_chat_files.py`).
- Test: `server/tests/test_tool_gating.py`, existing `test_api.py`, `test_agent_loop.py`, `test_permissions.py`

**Interfaces:**
- Consumes: Task 1 `Addons.tool_ready`, `Addons.level_addon`, `ABOUT`, `TOOL_ADDONS`, `LEVEL_ADDONS`; Task 2 `registry.addons = addons`.
- Produces (`base.py`): `class AddonGate(Protocol): def tool_ready(self, tool_name: str) -> bool`; `ToolRegistry.addons: AddonGate | None` (set by the app; None means no gate); `ToolRegistry.is_available(self, tool: Tool) -> bool`, which is `_is_available(tool)` and, when `addons` is set, `addons.tool_ready(tool.name)`. A gate that raises counts as not available. `available()` uses it. The GitHub tools keep their own `is_available` (`github_ready`) as well.
- Produces (`loop.py`): `NOT_READY = "This tool isn't set up on this PC. Open Settings › Add-ons to set it up."`. `_decide` and the after-approval re-check call `self._registry.is_available(tool)`. A tool that's unavailable at call time is denied through `_deny(tool, NOT_READY)`, with the same `tool.denied` audit detail as today. A level of Off keeps `TURNED_OFF`.
- Produces (`routes_settings.py`):
  - `_level_keys` = `DEFAULT_LEVELS` keys, then the registered tools' keys not already listed. Keys that exist only as stored rows are never listed, and `_resolve_key` answers 404 for them.
  - `_item` adds `"addon": {"id", "state", "ready", "short", "message"} | None` from `hk.addons.level_addon(key)`.
  - The row's `description` falls back to `ABOUT[addon][1]` when no tool with that key is registered (Images without ComfyUI).

- [ ] **Step 1: Write the failing tests**

```python
def test_registry_gate_hides_tool_and_loop_denies(...)      # gate says no → not in available(); a call → NOT_READY, run() not called
async def test_python_not_offered_after_docker_stops(harness):
    sandbox = FakeSandbox()                                     # docker up
    client = signed_in(harness, scripts=[python_call_after_round()], sandbox=sandbox)
    assert "python" in offered_tool_names(harness.chat, round=0)
    sandbox.docker = False
    client.post("/api/addons/refresh", headers=CSRF)
    # next round: python not offered; the scripted call is denied with NOT_READY and never reaches the sandbox
    assert sandbox.calls == [] and step_content(harness) == json.dumps({"error": NOT_READY})
async def test_token_removed_mid_round_denies_github_call(tmp_path):
    # AgentLoop with a registry gated by Addons over Probes whose token_saved reads a flag;
    # the fake chat client offers repo_read, clears the flag while streaming, then yields a repo_read call
    events = await run_turn(loop)
    assert offered[0] contains "repo_read" and repo_read_tool.runs == 0
    assert finished_step(events)["content"] == json.dumps({"error": NOT_READY})
    assert audit_events(conn, "tool.denied")[-1]["detail"]["tool"] == "repo_read"
def test_stored_levels_for_removed_tools_are_harmless(harness):
    c = harness.conn()
    for key, level in (("wallet", "off"), ("news", "allowed"), ("demo", "ask")):
        set_level(c, key, ToolLevel(level))
    c.commit(); c.close()
    client = signed_in(harness, registry=registry_with(harness.send))   # production-like: no demo tools
    keys = [row["tool"] for row in client.get("/api/settings/tools").json()]
    assert not {"wallet", "news", "demo"} & set(keys) and "images" in keys
    assert put_level(client, "wallet", "allowed").status_code == 404
    assert harness.query("SELECT COUNT(*) FROM tool_levels WHERE tool IN ('wallet','news','demo')") == [(3,)]
    # a turn still runs to turn_done
def test_tools_rows_carry_addon_state(harness):              # no sandbox → python row addon.short "Needs Docker", ready False
def test_images_row_without_comfyui_has_a_description(harness)  # description == ABOUT["images"][1], addon.state "not_set_up"
def test_build_registry_has_no_demo_tools():
    assert [t.name for t in cli.build_registry().all()] == []
@pytest.mark.real_levels
def test_default_levels_have_no_placeholders():
    assert not {"wallet", "news", "demo"} & set(permissions.DEFAULT_LEVELS)
```

`harness.start` gains `registry=None` (default: today's `registry_with(ClockTool(), NoteTool(), self.send)`).

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_tool_gating.py -q`
Expected: FAIL

- [ ] **Step 3: Implement the gate, the loop change, the level table, the registry without demo tools and the Tools rows; move `demo.py` into the tests and fix the imports; update the two docs lines**

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check . ../scripts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server docs
git commit -m "feat(tools): offer a tool only when its add-on is ready; drop the demo and placeholder levels"
```

---

### Task 5: Messages name a screen, never a command

**Files:**
- Modify:
  - `server/src/hearthkeep/tools/python.py` (`NO_DOCKER`, `NO_IMAGE`);
  - `server/src/hearthkeep/sandbox/runner.py` (`NO_IMAGE_MESSAGE`);
  - `server/src/hearthkeep/github/deps.py` (`REPO_NO_IMAGE`);
  - `server/src/hearthkeep/providers/scheduled_chat.py` (`_NOT_STARTED`);
  - `server/src/hearthkeep/providers/chat.py` (`_REJECTED`);
  - `server/src/hearthkeep/runtime/supervisor.py` (`_start_failed`, the port-in-use message, `_require`, the `model_dirs` message in `build_llama_process`).
- Modify tests that pin the old copy: `test_python_tool.py` (the `NO_DOCKER ==` and `NO_IMAGE ==` asserts), and any assert on the old texts in `test_read_pages_tool.py`, `test_repo_deps.py`, `test_scheduled_chat.py`, `test_chat_provider.py` and `test_supervisor.py` (find them with `grep -n "sandbox build\|llama-server.log\|secrets set\|<data>/models.toml" tests/*.py`).
- Test: `server/tests/test_owner_copy.py`

**Interfaces:**
- Produces (exact copy):
  - `python.NO_DOCKER = "Python analysis needs Docker Desktop running on this PC. Open Settings › Add-ons to set it up."`
  - `python.NO_IMAGE = "Python analysis needs the analysis sandbox, which isn't built yet. Open Settings › Add-ons to set it up."`
  - `runner.NO_IMAGE_MESSAGE = "Running Python needs the sandbox image, which isn't built yet. Open Settings › Add-ons to set it up."`
  - `deps.REPO_NO_IMAGE = "Installing or testing a repo needs the repo sandbox image, which isn't built yet. Open Settings › Add-ons to set it up."`
  - `scheduled_chat._NOT_STARTED = "The chat model couldn't be started. Open Settings › Add-ons to check it."`
  - `chat._REJECTED = "The chat server rejected Hearthkeep's key. Check the chat model in Settings › Add-ons."`
  - `ManagedProcess._start_failed`: `ProcessError(f"{self.name} couldn't start. Open Settings › Add-ons to check it.", detail)`. The log location moves into `detail` (`f"log: logs/{self.log_path.name}"`, relative, never absolute), so the console and server log still say where to look. `_log_hint` is removed if nothing else uses it.
  - Port in use: `f"Port {self.port} is already in use by another program. Close that program, or change the port in models.toml in Hearthkeep's data folder."`
  - `_require`: `f"Set {key} in models.toml in Hearthkeep's data folder first."`. The `model_dirs` message: `f'Set model_dirs."{card.id}" in models.toml in Hearthkeep\'s data folder to the folder with the {card.name} files.'`
- `python.unavailable_message` keeps mapping by identity with the runner constants.

- [ ] **Step 1: Write the failing test**

```python
FORBIDDEN = (re.compile(r"\bhearthkeep (serve|sandbox|secrets|startup|enroll-code)\b"),
             re.compile(r"<data>|<home>|--data\b|\buv run\b"))

@pytest.mark.parametrize("text", [
    python.NO_DOCKER, python.NO_IMAGE, runner.NO_DOCKER_MESSAGE, runner.NO_IMAGE_MESSAGE,
    runner.DATA_PATH_MESSAGE, deps.REPO_NO_DOCKER, deps.REPO_NO_IMAGE, scheduled_chat._NOT_STARTED,
    chat._REJECTED, chat._UNREACHABLE, chat.NO_CHAT_MODEL, loop.NOT_READY, loop.TURNED_OFF,
])
def test_owner_messages_have_no_commands_or_placeholders(text):
    assert not any(p.search(text) for p in FORBIDDEN)

def test_process_start_failure_names_a_screen(tmp_path):      # ManagedProcess with data_dir=tmp_path → user_message has
    err = ManagedProcess("llama-server", ["x"], "http://127.0.0.1:1/health",   # "Settings › Add-ons",
                         tmp_path / "logs" / "llama-server.log", data_dir=tmp_path)._start_failed()
    assert "Settings › Add-ons" in err.user_message and not any(p.search(err.user_message) for p in FORBIDDEN)
    assert str(tmp_path) not in str(err)
def test_supervisor_config_messages_have_no_placeholder()     # _require and build_llama_process without model_dirs
```

- [ ] **Step 2: Run it to see it fail**

Run: `cd server && uv run pytest tests/test_owner_copy.py -q`
Expected: FAIL (`NO_IMAGE` contains "hearthkeep sandbox build")

- [ ] **Step 3: Change the copy and update the tests that pinned the old texts**

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check . ../scripts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "fix(copy): failure messages point to Settings › Add-ons instead of commands"
```

---

### Task 6: Web: the add-ons client and Settings › Add-ons

**Files:**
- Create: `web/src/api/addons.ts`, `web/src/shell/AddonsContext.tsx`, `web/src/components/AddonsPanel.tsx`, `web/src/styles/addons.css`, `web/src/test/addons.tsx`
- Modify:
  - `web/src/shell/AppShell.tsx` (one `useAddons()`, the provider, `openSettings`);
  - `web/src/views/Settings.tsx` (`AddonsPanel` first; `focus` prop; section ids; the `StartupPanel` section gets `id="settings-startup"`);
  - `web/src/components/GitHubPanel.tsx` (`id="settings-github"`);
  - `web/src/components/FoldersPanel.tsx` (`id="settings-folders"`);
  - `web/src/main.tsx` (imports `addons.css`);
  - `web/src/test/fetch.ts` (fixtures);
  - `web/src/shell/views.ts` (the Settings blurb leads with "Add-ons");
  - `docs/install.md` (§11 and §12 each gain one line: Settings › Add-ons shows whether the add-on is ready).
- Test: `web/src/__tests__/addons.test.tsx`, existing `settings.test.tsx`, `shell.test.tsx`

**Interfaces:**
- Consumes: Task 2 routes.
- Produces (`api/addons.ts`):
  - `type AddonId = 'chat' | 'analysis' | 'images' | 'github' | 'devices' | 'startup'`;
  - `type AddonAction = 'install' | 'start_docker' | 'build_image' | 'add_token' | 'open_setup' | 'turn_on'`;
  - `type Addon = { id: AddonId; name: string; about: string; requires: string; state: string; ready: boolean; message: string; action: AddonAction | null; short: string | null; details: { model?: string; loaded?: boolean } }`;
  - `type AddonsResponse = { addons: Addon[]; checked_at: string | null }`;
  - `getAddons(): Promise<AddonsResponse>` and `refreshAddons(): Promise<AddonsResponse>` (`POST /api/addons/refresh`);
  - `useAddons(): AddonsState`, where `AddonsState = { addons: Addon[] | null; failed: boolean; refreshing: boolean; reload(): void; refresh(): Promise<void> }`. It polls every 15 s and on `visibilitychange`, like `useHealth`. A failed load leaves `addons` as it was (null at first).
- Produces (`shell/AddonsContext.tsx`):
  - `type SettingsAnchor = 'settings-addons' | \`addon-${AddonId}\` | 'settings-github' | 'settings-folders' | 'settings-startup'`;
  - `AddonsProvider({ value, children })`, where `value: AddonsState & { openSettings(anchor: SettingsAnchor): void }`;
  - `useAddon(id: AddonId): Addon | null`, which is null when unknown (no provider, still loading, or failed). **Unknown never gates anything**: views behave as today;
  - `useAddonsState(): AddonsState | null`;
  - `useOpenSettings(): (anchor: SettingsAnchor) => void` (a no-op without a provider);
  - `actionTarget(addon: Addon): SettingsAnchor`: `add_token` → `'settings-github'`, `turn_on` → `'settings-startup'`, anything else → `` `addon-${addon.id}` ``.
- Produces (`AppShell`): `openSettings(anchor)` selects `settings` and keeps the anchor in state. It renders `Settings({ focus, onFocused }: { focus?: SettingsAnchor | null; onFocused?: () => void })`. Settings scrolls `document.getElementById(focus)` into view and focuses it (`tabIndex={-1}` on each target) once rendered, then calls `onFocused()`, which clears the anchor so a later visit doesn't jump.
- Produces (`AddonsPanel`):
  - `<section id="settings-addons" aria-labelledby=…>` with `<h2>Add-ons</h2>` and a "Check again" button (`aria-busy` while `refreshing`; on failure the alert "Couldn't check the add-ons. Try again.").
  - One `<article id={`addon-${id}`} tabIndex={-1}>` per add-on: `<h3>{name}</h3>`; a badge (`ready` → "Ready" green, `on` → "On" green, `off` → "Off", `unsupported` → "Only on Windows", `this_pc_only` → "This PC only", `starting` → "Loading", `offline` → "Offline", `error` → "Needs attention", otherwise "Not set up"); `about`; "Needs: {requires}"; and `message` when not ready.
  - The action button, when it leads somewhere that acts: `add_token` → "Add a GitHub token" (`openSettings('settings-github')`); `turn_on` → "Start at login settings" (`'settings-startup'`). The analysis card always has a "Shared folders" link button to `'settings-folders'`.
  - The panel's own `addons` come from `useAddonsState()`. Without a provider it loads once itself, so Settings works standalone in tests.

- [ ] **Step 1: Write the failing tests**

```tsx
it('lists the six add-ons with state, needs and message', ...)       // mockFetch GET /api/addons → addonsWith({analysis: 'docker_stopped'})
it('check again posts refresh and shows the new state', ...)          // POST /api/addons/refresh called with X-Hearthkeep; badge "Ready"
it('add token goes to the GitHub panel', ...)                         // github needs_token → button "Add a GitHub token" → #settings-github focused
it('analysis links to shared folders', ...)
it('openSettings from the shell focuses the add-on card', ...)        // AppShell: call openSettings('addon-images') via a test consumer → heading Settings, #addon-images has focus
it('unknown add-ons never gate: a 404 leaves views as today', ...)   // useAddon returns null when /api/addons fails
it('never shows a command or placeholder', ...)                       // container text has no "hearthkeep " / "<data>"
```

`test/fetch.ts` gains `addonsReady: Reply` (all six ready, chat `{model: 'Local model 12B', loaded: true}`) and `addonsWith(states: Partial<Record<AddonId, string>>): Reply`, which fills message, action and short from a small table matching Task 1's copy. `test/addons.tsx` gains `renderWithAddons(ui, addons: Addon[], openSettings = vi.fn())`.

- [ ] **Step 2: Run them to see them fail**

Run: `cd web && npx vitest run src/__tests__/addons.test.tsx`
Expected: FAIL (cannot resolve `../api/addons`)

- [ ] **Step 3: Implement the client, the context, the panel, the shell wiring and the section ids, then the two docs lines** (dark tokens from `tokens.css`; reuse `.settings-section`, `.tile`, `.badge`, `.note`; `addons.css` only for the card grid)

- [ ] **Step 4: Run the web suite and build**

Run: `cd web && npm test && npm run build`
Expected: PASS, build without type errors

- [ ] **Step 5: Commit**

```bash
git add web docs
git commit -m "feat(web): Settings › Add-ons shows each add-on's state and what it needs"
```

---

### Task 7: Web: the composer, the top bar and every view follow their add-on

**Files:**
- Create: `web/src/components/SetUpPanel.tsx`
- Modify:
  - `web/src/shell/Composer.tsx` (`blocked`, `notice`);
  - `web/src/views/Chat.tsx` (chat gating, the loading text, the 409, reloading add-ons after a turn);
  - `web/src/shell/TopBar.tsx` (the pill from chat state; the `Health` type gains `'not_set_up'`);
  - `web/src/views/Images.tsx` (the panel replaces the form and the old `images-setup` block);
  - `web/src/views/Analysis.tsx` (the panel; the empty state's button);
  - `web/src/views/Repos.tsx` (the panel replaces both notes and the add form; `RepoItem` gains `canUpdate`);
  - `web/src/components/GitHubPanel.tsx` (the no-Git note gains a "Set up" button);
  - `web/src/views/ToolsPermissions.tsx` (the add-on flag; drop the placeholder copy and names; passkey wording);
  - `web/src/components/ModelList.tsx` (an image card that isn't managed shows "Not set up");
  - `web/src/shell/AppShell.tsx` (`Analysis` gets `onStartChat`).
- Modify tests that pin the old copy:
  - `shell.test.tsx` (Files still shows its placeholder; that's project 3);
  - `permissions.test.tsx` (wallet, news and demo rows; "Arrives in a later update"; "Needs Docker to run freely"; "Face ID / Touch ID");
  - `images.test.tsx` (the old not-set-up texts).
- Test: `web/src/__tests__/gating.test.tsx`, plus the files above and `chat.test.tsx`, `analysis.test.tsx`, `repos.test.tsx`, `models.test.tsx`

**Interfaces:**
- Consumes: Task 6 `useAddon`, `useOpenSettings`, `actionTarget`, `useAddonsState`, `renderWithAddons`; Task 3 409 `chat_not_set_up`; Task 4 tool rows' `addon`.
- Produces (`SetUpPanel`): `SetUpPanel({ addon }: { addon: Addon })`, a `<section className="setup-panel" aria-label={`Set up ${addon.name}`}>` holding `about` (as the title), "Needs: {requires}", `message`, and a primary button `Set up ${addon.name}` that calls `openSettings(actionTarget(addon))`. It's the only panel any view uses for this.
- Produces (`Composer`): new props `blocked?: { message: string; onSetUp?: () => void } | null` and `notice?: string | null`.
  - With `blocked`: the textarea and Send are disabled, and a visible `<p id="composer-state" className="composer-state">` shows the message plus a "Set up" button when `onSetUp` is given. The textarea gets `aria-describedby="composer-state"`.
  - With `notice`: the same paragraph with `role="status"`, and the composer stays usable.
- Produces (`Chat`): `chat = useAddon('chat')`.
  - `not_set_up`, `offline` and `error` give `blocked = { message: chat.message, onSetUp: () => openSettings('addon-chat') }`.
  - `starting` gives `notice = chat.message` ("Loading the chat model…").
  - The quiet typing indicator of the live turn shows the visible text "Loading the chat model…" next to the dots when `chat?.state === 'starting' || chat?.details.loaded === false`.
  - `sendError` maps `chat_not_set_up` to the server's `message`.
  - When a turn ends, `useAddonsState()?.reload()`.
- Produces (`TopBar`): `StatusPill({ health, chat }: { health: HealthState; chat: Addon | null })`. `unreachable` and `checking` come from health as today. Otherwise, when `chat` is known, the visible text and `data-state` follow it:

| chat | text | data-state |
|---|---|---|
| `not_set_up` | No chat model | `not_set_up` |
| `starting` | Loading the chat model… | `standby` |
| `offline` | Chat model offline | `offline` |
| `error` | Chat model needs attention | `offline` |
| `ready`, loaded | `{details.model}` | `online` |
| `ready`, not loaded | `{model} · starts when needed` | `standby` |

  When `chat` is unknown, the pill keeps today's health-based behaviour, plus `not_set_up` → "No chat model". The `title` tooltip stays, as extra detail only.
- Produces (views):
  - `Images`: `images = useAddon('images')`; known and not ready gives `<SetUpPanel addon={images}/>` in place of the describe form. The gallery still shows when it has images. The `images-setup` block and its "See docs/models.md" line are removed.
  - `Analysis({ onOpenChat, onStartChat })`: `analysis` known and not ready gives `SetUpPanel` above the list. When it's ready (or unknown) and there are no chats, the empty state keeps `ANALYSIS_EMPTY` and adds the button "Start a chat with a file" → `onStartChat()`. AppShell passes `() => select('chat')`.
  - `Repos`: `github` known and not ready gives `SetUpPanel` in place of the two notes and the add form. Added repos are still listed, with `canUpdate={false}`, which hides Update.
  - `GitHubPanel`: the no-Git note keeps its text and gains a button "Set up" → `openSettings('addon-github')`.
- Produces (`ToolsPermissions`):
  - `ToolSetting.addon?: { id: AddonId; state: string; ready: boolean; short: string | null; message: string } | null`.
  - A row whose `addon` isn't ready shows `<p className="perm-flag">{short} — <button className="link">Set up</button></p>`; the button calls `openSettings(`addon-${id}`)`. The visible text reads, for example, "Needs Docker — Set up".
  - The `installed`/"Arrives in a later update" branch and the "Needs Docker to run freely" flag are removed. `NAMES` loses `wallet`, `news` and `demo`. Both "Face ID / Touch ID" strings say "your passkey".

- [ ] **Step 1: Write the failing tests**

```tsx
it('composer is disabled with visible text when no chat model is set up', ...)  // textbox disabled; "Set up your chat model to start chatting." visible; Set up → openSettings('addon-chat')
it('offline chat server disables the composer with visible text, not a tooltip', ...)
it('first send while the model is not loaded shows "Loading the chat model…"', ...)  // chat ready, loaded false; typing area has the text
it('a 409 chat_not_set_up shows the server message', ...)
it('pill follows the chat add-on', ...)                       // not_set_up → "No chat model"; ready not loaded → "… · starts when needed"
it('pill keeps health behaviour when add-ons are unknown', ...)   // existing shell tests unchanged
it('images without its add-on shows one set-up panel and no form', ...)  // no textbox "Describe the image you want"; button "Set up Images" → addon-images
it('analysis empty state has a Start a chat with a file button', ...)
it('analysis without Docker shows the set-up panel above saved charts', ...)
it('repos needing a token shows the set-up panel, no add form, no Update', ...)  // "Set up GitHub" → openSettings('settings-github') (actionTarget of add_token)
it('tool rows show "Needs Docker — Set up" and no placeholder rows', ...)  // no "Arrives in a later update", no "Wallet", no "Face ID"
it('an unmanaged image card is listed as Not set up', ...)
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd web && npx vitest run src/__tests__/gating.test.tsx src/__tests__/chat.test.tsx src/__tests__/permissions.test.tsx`
Expected: FAIL

- [ ] **Step 3: Implement `SetUpPanel`, the composer and pill changes, and the view changes; update the tests that pinned the old copy**

- [ ] **Step 4: Run the web suite, the build and the server suite**

Run: `cd web && npm test && npm run build && cd ../server && uv run pytest -q`
Expected: PASS, build without type errors

- [ ] **Step 5: Check by hand on the reference host**

Run `cd server && uv run hearthkeep serve --data <your data folder>` (your own folder; this is a developer step, not owner copy). Then:
- stop Docker Desktop, choose "Check again" in Settings › Add-ons, and see Analysis go to "Not set up" with the start message, Tools & permissions show "Needs Docker — Set up" on Python, and the Analysis view show the panel;
- start Docker again, check again, and see it come back.

Expected: each screen matches the add-on state, with no command or placeholder anywhere.

- [ ] **Step 6: Commit**

```bash
git add web
git commit -m "feat(web): chat, Images, Analysis, Repos and tool rows follow their add-on's state"
```

---

## Self-review notes (spec §4 against this plan)

- §4.1 registry, six ids, states, an explanation and a fix-action id per state, no command or placeholder: Task 1 (copy table and test). Task 5 covers the messages outside the registry.
- §4.2 tools not offered and denied at call time: Task 4. Screens use one set-up panel: Task 7 (Images, Analysis, Repos). Files search is project 3's Files view. Tools & permissions shows only real tools with their state: Tasks 4 and 7. Messages name a screen: Task 5. Freshness, cache and checks off the loop: Tasks 1 and 2.
- §4.3 no chat model: the server starts and the composer is disabled with the message (Tasks 3 and 7). "Opens to Today with a card" belongs to project 3, since Today stays a placeholder here. Offline and starting are shown in visible text, and "Loading the chat model…" appears on first send: Task 7.
- §6a: today's formats are read as they are and tested against copies (Task 3 fixtures; Task 4 stored levels).
