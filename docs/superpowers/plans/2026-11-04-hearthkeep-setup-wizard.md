# Hearthkeep Phase 5, Project 2c: Setup Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After double-clicking `Setup.cmd`, a person walks through a browser wizard: what Hearthkeep is, their PC and where to keep its data, a chat model that fits, the add-ons they want (with Start at login on by default), one progress list for every download, their passkey, and Done. Nothing is typed into a console. The same start-at-login switch and sandbox build then live in Settings, so the read-only command text goes away.

**Architecture:** The 2a setup server already runs the whole app in setup mode, but every model and add-on route needs a session that can't exist before the first passkey. 2c adds a setup-secret gate as a FastAPI dependency and a small set of POST-only `/api/setup/*` routes that call the same service functions as the session routes (the 2b install job, the chat switch, start at login, the new sandbox build job), with the setup secret standing in for step-up. Choosing another data folder ends the setup server with a new exit code; `cmd_setup` loops and starts it again in the new folder with the same secret, and the wizard simply waits for it. The wizard is a stepper inside the existing `/setup` page and reuses the 2b `DownloadList` and model rows through an API adapter, so Settings and the wizard share one set of components.

**Tech Stack:** The Phase 1–5 stack (Python 3.12.6/uv, FastAPI, httpx, pytest, ruff; React 19 + TypeScript, Vitest). Windows Task Scheduler (`schtasks`) and Docker CLI as today. No new dependency.

**Spec:** `docs/superpowers/specs/2026-11-03-hearthkeep-phase-5-design.md` §5.2 (setup mode), §5.3 steps 1–4 and 6–8 (this project), §5.5, §6 "Settings › Add-ons" (build image, turn on, step-up for widening) and §6a "Start at login: a real on/off switch". Audit notes U11 and U12 (`.superpowers/phase5-audit-notes.md`). Builds on 2a (`SetupMode`, `/api/setup` gate, `cmd_setup`, `check_data_dir`, `host.enable_startup`) and 2b (`Installer`, `PcCache`, `recommend`, `routes_models`, `DownloadList`, `ModelsPanel`). **Out of scope, moved to 2d:** step 5 Remote access (Tailscale detection, `tailscale serve`, the HTTPS origin, re-enrolment when it changes later, §6.3) and the Advanced settings form (§6a). The 2c wizard's Phone card says phone access is set up later in Settings and links nowhere yet.

## Global Constraints

- **Loopback only; passkeys only.** Setup routes accept only a loopback TCP peer, an open setup (no credential yet), the rate limit and the setup secret, in the 2a order. The secret travels only in POST bodies; no setup route is a GET. The enrolment code still comes only from `/api/setup/enrollment-code`.
- **The setup secret stands in for step-up, and only while setup is open.** It proves the person is at the PC (§5.2). Once the first passkey exists every `/api/setup/*` route is 404 and the same actions need a session and, where they widen, a step-up (`download`, `settings`) as in 2b.
- **Widening needs step-up in Settings** (§6): turning start at login on and building the sandbox image take a step-up with purpose `settings`; turning start at login off needs only the session. All are audited (`startup.enabled`, `startup.disabled`, `sandbox.build_started`, `sandbox.build_finished`, plus `*_denied`); setup-mode audit rows use actor `"setup"`.
- **Downloads keep every 2b rule:** catalog ids only, the HTTPS allow-list, SHA-256, resumable, one job at a time.
- **Existing installs are never re-set-up.** `cmd_setup` on a folder with a passkey still only opens the app (2a). Data-folder moves are allowed only for a folder this setup run created and before any download has started.
- **No duplicated logic.** Setup and session routes call the same functions; the wizard and Settings render the same components. Copy that both show is defined once.
- **Copy rules.** "passkey", plain words, screen names, no CLI command, placeholder or path in owner text (a folder path the owner typed or the default `C:\Hearthkeep\data` shown as a value is fine). Console text stays ASCII. `forbidden_in` / `forbiddenIn` cover every new string.
- **Windows is the only host.** On other OSes the setup routes for folders and start at login answer `unsupported` and the wizard hides those controls.
- **Nothing personal in the repo; no AI attribution in commits.**
- **Commands.** Server, from `server/`: `uv run pytest -q`, `uv run ruff check . ../scripts`, `uv run ruff format --check <files>`. Web, from `web/`: `npx vitest run <file>`, `npm test`, `npm run build`. `python scripts/privacy_scan.py --staged` before each commit.

## Review Focus

1. **A second local process tries to use the new setup routes.** Without the secret, with a wrong one, from a non-loopback peer, after the first passkey exists, or without the CSRF header: 403/404, nothing started, nothing written, and the secret in no log, audit row or response. (Task 1: `test_every_setup_route_uses_the_gate`, parametrised over every route.)
2. **The data folder is changed after something was downloaded, or to a bad folder.** A move after an install job started is 409 `too_late`; a bad folder gets the 2a sentence (`TOO_LONG`, `BAD_CHARACTERS`, `NOT_EMPTY`…) and nothing restarts; a good move restarts setup in the new folder with the same secret, and the old folder (created by this run, holding nothing but what setup made) is removed. (Task 2.)
3. **The wizard page is reloaded or the setup server restarts mid-way** (folder move, a crash). The page keeps its step (stored without the secret), waits for `/api/health`, re-checks status with the secret it still holds, and carries on; a running download shows its progress again. (Task 5.)
4. **Start at login fails or isn't supported.** `schtasks` refuses or times out: the wizard and Settings show a plain sentence and the switch stays off; setup still finishes. On another OS the control isn't shown. (Task 3.)
5. **Docker isn't installed, isn't running, or the build fails half-way.** The Analysis card says which, with "Get Docker Desktop" (official page) and "Check again"; a failed build shows a plain sentence and Retry, keeps the last 40 output lines for the log file only (never in a response), and never blocks the rest of setup. (Task 4.)

## Calls made in this plan

- **2c/2d split.** Remote access (wizard step 5 and the later change with re-enrolment) and the Advanced form are a separate project, 2d: they share Tailscale code and the origin change, which is the riskiest part of Phase 5. Until 2d lands, the wizard's Phone card reads "You can add your phone later, from Settings." and install.md §7 stays the way to choose remote access before the first passkey.
- **Setup routes are POST-only under `/api/setup/`**, each taking `{secret, …}`: `pc`, `catalog`, `install`, `install/status`, `install/cancel`, `install/retry`, `chat`, `addons`, `startup`, `sandbox/build`, `sandbox/status`, `data-folder`. Reads are POST so the secret stays out of URLs.
- **The gate becomes a dependency,** `SetupDep`, used by every setup route, so the 2a order (open → loopback → rate limit → secret) is enforced in one place. The setup rate group stays `"setup"`, with a higher per-client limit (120 per minute) so the wizard's 1-second progress polling fits; the global cap stays.
- **Data-folder move:** `POST /api/setup/data-folder {secret, path}` checks the path with `host.check_data_dir` and needs at least 5 GB free on that drive (`MIN_FREE_GB = 5`); then it records the new folder on `SetupMode.next_data_dir` and asks the server to stop with the new `EXIT_RELOCATE = 76`. `cmd_setup` loops: it removes the old folder only when this run created it and it holds nothing but setup's own files (`config.toml`, `hearthkeep.db*`, the empty subfolders), writes the locator for the new folder, and runs setup again there with the same `SetupMode`. The browser is not reopened on a relocate.
- **Start at login in setup is a choice, applied after the passkey.** The wizard sets `SetupMode.start_at_login` (default `True`, §5.3 "nothing is chosen by default except Start at login"); after the hand-over, `cmd_setup` calls `host.enable_startup(data)` when it's true and prints one ASCII line if it failed (setup still succeeds).
- **Sandbox build job** (`sandbox/builder.py`): builds the two images `cli.cmd_sandbox_build` builds, in a thread, one at a time, with output captured to a 40-line ring kept for `logs/sandbox-build.log` only. States `idle | building | done | failed`, reasons `docker_missing | docker_stopped | build_failed`. `cmd_sandbox_build` reuses it so the CLI and the app build the same way.
- **One component set, two API adapters.** `ModelsPanel`'s rows, confirm box and `DownloadList` take a `ModelsApi` object (`getCatalog`, `startInstall`, `getJob`, `cancel`, `retry`, `switchLocal`, `switchExternal`); Settings passes the session adapter (step-up inside), the wizard passes the setup adapter (secret inside). No second model UI.
- **Wizard state:** the current step index lives in `sessionStorage` under `hk.setup.step` (never the secret). Choices that matter (chat model, add-ons, start at login, data folder) live on the server, so a reload or a relocate loses nothing.
- **U12:** the sign-in hint becomes "Your setup code comes from Settings › Devices on a device that's already signed in, or from Setup.cmd on this PC."
- **"Try this" examples on Done** are project 6 (demos). 2c's Done opens the app.

---

## File structure

```
server/src/hearthkeep/api/routes_setup.py      # MODIFY: SetupDep + the new POST routes
server/src/hearthkeep/api/routes_models.py     # MODIFY: route bodies move to service functions both route sets call
server/src/hearthkeep/models/service.py        # NEW: catalog_view, start_install, switch_chat (shared by session and setup routes)
server/src/hearthkeep/setupmode.py             # MODIFY: next_data_dir, start_at_login
server/src/hearthkeep/lifecycle.py             # MODIFY: EXIT_RELOCATE = 76
server/src/hearthkeep/cli.py                   # MODIFY: cmd_setup loop + start at login after hand-over; cmd_sandbox_build uses the builder
server/src/hearthkeep/sandbox/builder.py       # NEW: sandbox build job
server/src/hearthkeep/api/routes_settings.py   # MODIFY: POST /api/settings/startup
server/src/hearthkeep/api/routes_addons.py     # MODIFY: POST /api/addons/analysis/build, GET …/build
server/src/hearthkeep/api/app.py               # MODIFY: Services.sandbox_builder; shutdown
web/src/api/setup.ts                           # MODIFY: setup adapter calls
web/src/api/models.ts                          # MODIFY: ModelsApi type + session adapter
web/src/components/ModelsPanel.tsx             # MODIFY: take a ModelsApi; export ChatModelPicker, ImageModelPicker
web/src/components/StartupSwitch.tsx           # NEW: shared switch (Settings + wizard)
web/src/components/SandboxBuild.tsx            # NEW: shared Analysis build control (Add-ons card + wizard)
web/src/views/SetupPage.tsx                    # MODIFY: the stepper
web/src/views/setup/*.tsx                      # NEW: one file per step
web/src/views/Settings.tsx                     # MODIFY: StartupPanel uses StartupSwitch
web/src/components/AddonsPanel.tsx             # MODIFY: build_image action → SandboxBuild
web/src/views/SignIn.tsx                       # MODIFY: U12 hint
docs/install.md, README.md                     # MODIFY (Task 6)
```

---

### Task 1: The setup gate as a dependency and setup routes for models

**Files:**
- Modify: `server/src/hearthkeep/api/routes_setup.py`, `server/src/hearthkeep/api/routes_models.py`
- Create: `server/src/hearthkeep/models/service.py`
- Test: `server/tests/test_setup_routes.py` (new); `server/tests/test_setup_mode.py` and `server/tests/test_api_model_install.py` keep passing

**Interfaces:**
- Consumes: 2a `SetupMode`, `setup_open`, `client_ip`, `rate_limit`; 2b `Installer`, `PcCache`, `recommend`, `fits`, `chat_state`, `_switch_external` and the routes' error mapping.
- Produces:
  - `class SetupBody(BaseModel): secret: str = Field(min_length=1, max_length=128)`; every new body subclasses it.
  - `async def setup_gate(request: Request, body: SetupBody) -> None` and `SetupDep` (the 2a `_gate` moved behind a dependency; same order, same audit `setup.denied`).
  - `models/service.py`: `async def catalog_view(hk) -> dict` (today's `GET /api/models/catalog` body), `def start_install(hk, items: list[str], make_default: bool, actor: str) -> Job` (raises the same `ApiError`s as today: `unknown_item`, `doesnt_fit`, `busy`, `no_space`, `nothing_to_do`), `async def switch_chat(hk, body: LocalChat | ExternalChat, actor: str) -> dict`. The session routes call them after their own step-up check; nothing else changes in their responses.
  - Setup routes (all `POST`, all `SetupDep`, rate group `"setup"` at 120/min per client): `/api/setup/pc` → `{pc, data_dir, data_dir_default: bool, can_move_data: bool}`; `/api/setup/catalog` → `catalog_view`; `/api/setup/install {items, make_default}` → 202 `{job}`; `/api/setup/install/status` → `{job}`; `/api/setup/install/cancel`; `/api/setup/install/retry`; `/api/setup/chat` (same bodies as `/api/models/chat` minus `step_up_token`). Audit actor `"setup"`.

- [ ] **Step 1: Write the failing tests** in `test_setup_routes.py` with the existing harness in setup mode:
  - `test_every_setup_route_uses_the_gate` — parametrised over every new route: no secret → 422/403, wrong secret → 403 `setup_secret` (and `setup.denied` audited), non-loopback peer → 403 `loopback_only`, after a credential exists → 404, no CSRF header → 403 `csrf`; the secret appears in no response, audit row or log record.
  - `test_setup_catalog_matches_the_session_catalog` — the same dict as `GET /api/models/catalog` for a signed-in session on the same app.
  - `test_setup_install_starts_a_job_without_step_up` and `test_setup_install_maps_the_same_errors` (busy, no_space, unknown_item, doesnt_fit, nothing_to_do).
  - `test_setup_polling_fits_the_rate_limit` — 70 `install/status` calls in a minute succeed; the 121st is 429.
  - `test_session_routes_unchanged` — the 2b route tests still pass (run them).

- [ ] **Step 2: Run** `uv run pytest -q tests/test_setup_routes.py` — FAIL. **Step 3: Implement** (move bodies into `models/service.py` first, then add the routes). **Step 4: Run** `uv run pytest -q tests/test_setup_routes.py tests/test_setup_mode.py tests/test_api_model_install.py` — PASS.

- [ ] **Step 5: Commit** `feat(setup): setup routes for the PC check, the catalog and downloads`.

---

### Task 2: Choosing the data folder in the browser

**Files:**
- Modify: `server/src/hearthkeep/setupmode.py`, `server/src/hearthkeep/lifecycle.py`, `server/src/hearthkeep/cli.py` (`cmd_setup`), `server/src/hearthkeep/api/routes_setup.py`
- Test: `server/tests/test_setup_mode.py`, `server/tests/test_setup_routes.py`

**Interfaces:**
- Consumes: `host.check_data_dir`, `_is_data_folder`, `_protect_folder`, `host.write_location`, Task 1 `SetupDep`, `Installer.current()`.
- Produces:
  - `EXIT_RELOCATE = 76` in `lifecycle.py` (the launcher treats it like any non-restart code; it's only used by setup).
  - `SetupMode.next_data_dir: Path | None = None`, `SetupMode.created_folder: bool` (set by `cmd_setup`), `SetupMode.start_at_login: bool = True` (used in Task 3).
  - `POST /api/setup/data-folder {secret, path: str (≤ 260)}` → 409 `too_late` once any install job has started in this run; 400 `{reason: "bad_folder", message}` with the 2a sentence from `check_data_dir`; 400 `not_enough_space` `{free_gb, needed_gb: 5}`; otherwise 202 and `control.request(EXIT_RELOCATE)` after the response (BackgroundTask, as the 2a hand-over does).
  - `cmd_setup` becomes a loop around today's steps 3–14: on `EXIT_RELOCATE` with `setup.next_data_dir` set, it removes the old folder only if `setup.created_folder` and its contents are a subset of `{config.toml, hearthkeep.db, hearthkeep.db-wal, hearthkeep.db-shm, <DATA_SUBDIRS> (empty)}`, writes the locator for the new folder, and runs the steps again in the new folder with the same `SetupMode` (secret unchanged; no browser reopen; prints `MOVING_DATA = "Moving Hearthkeep's data to the folder you chose."`).

- [ ] **Step 1: Write the failing tests**: `test_move_restarts_setup_in_the_new_folder_with_the_same_secret` (fake `run_server` returns `EXIT_RELOCATE` once, then completes; the second build uses the new folder and the same secret; `open_url` called once), `test_old_folder_removed_only_when_setup_made_it_and_it_is_empty_of_owner_files` (an extra file keeps it), `test_move_refused_after_a_download_started` (409 `too_late`), `test_bad_folder_gets_the_2a_sentence` (parametrised over `TOO_LONG`, `BAD_CHARACTERS`, `NOT_EMPTY`, `NOT_LOCAL`), `test_move_needs_5_gb_free`, `test_move_copy_is_ascii_and_clean`.

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest -q tests/test_setup_mode.py tests/test_setup_routes.py` — PASS.

- [ ] **Step 5: Commit** `feat(setup): choose where Hearthkeep keeps its data from the browser`.

---

### Task 3: Start at login as a switch (setup and Settings), and the sign-in hint

**Files:**
- Modify: `server/src/hearthkeep/api/routes_settings.py`, `server/src/hearthkeep/api/routes_setup.py`, `server/src/hearthkeep/cli.py` (after the hand-over), `web/src/views/Settings.tsx`, `web/src/views/SignIn.tsx`
- Create: `web/src/components/StartupSwitch.tsx`
- Test: `server/tests/test_api_settings.py` (or the file that tests `/api/settings/startup`), `server/tests/test_setup_mode.py`, `web/src/__tests__/settings.test.tsx`, `web/src/__tests__/signin.test.tsx`

**Interfaces:**
- Consumes: `host.enable_startup(data_dir, *, run)`, `host.disable_startup(*, run)`, `host.startup_status`, `startup.StartupError`, `HostUnsupported`; `SetupMode.start_at_login`.
- Produces:
  - `POST /api/settings/startup {enabled: bool, step_up_token: str = ""}`: on → step-up purpose `settings`, then `host.enable_startup(settings.data_dir)` in a thread; off → session only, `host.disable_startup()`; answers the same shape as `GET /api/settings/startup` minus `command` (the `command` field is removed from both); errors: 400 `unsupported`, 502 `startup_failed` (plain message, no schtasks output). Audits `startup.enabled` / `startup.disabled` / `startup.enable_denied`. Refreshes the `startup` add-on.
  - `POST /api/setup/startup {secret, enabled}` sets `setup.start_at_login` only (nothing runs until the hand-over) and answers `{enabled}`.
  - `cmd_setup`, after the shortcut and before spawning the launcher: when `setup.start_at_login`, `host.enable_startup(data)`; on failure prints `STARTUP_NOT_TURNED_ON = "Start at login couldn't be turned on. You can turn it on in Settings."` and carries on.
  - `StartupSwitch({ state, onChange, busy, error })` — a labelled switch (`role="switch"`, `aria-checked`) with "Start Hearthkeep when I sign in to this PC", the "On, for another data folder" note, and the error line. Settings' `StartupPanel` uses it (no more command text: U11); the wizard uses it in Task 5.
  - U12 hint text as in Calls.

- [ ] **Step 1: Write the failing tests**: server `test_turning_startup_on_needs_step_up_and_off_does_not`, `test_startup_failure_is_a_plain_502`, `test_startup_command_is_gone`, `test_setup_startup_choice_applied_after_the_passkey` (fake host records `enable_startup` once, after the hand-over), `test_setup_finishes_when_startup_fails`; web `settings startup switch turns on with a passkey and off without`, `no command text is shown`, `the sign-in hint names Settings › Devices` — every new string through `forbiddenIn`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** the named server and web tests — PASS.
- [ ] **Step 5: Commit** `feat(startup): a real start-at-login switch in Settings and in setup`.

---

### Task 4: Building the analysis sandbox from the app

**Files:**
- Create: `server/src/hearthkeep/sandbox/builder.py`, `web/src/components/SandboxBuild.tsx`
- Modify: `server/src/hearthkeep/api/routes_addons.py`, `server/src/hearthkeep/api/routes_setup.py`, `server/src/hearthkeep/api/app.py`, `server/src/hearthkeep/cli.py` (`cmd_sandbox_build` uses the builder), `server/src/hearthkeep/sandbox/image.py` (`build_image` gains an `on_line` callback), `web/src/components/AddonsPanel.tsx`
- Test: `server/tests/test_sandbox_builder.py` (new), `server/tests/test_addons_api.py` (or the existing add-ons API test), `web/src/__tests__/addons.test.tsx`

**Interfaces:**
- Consumes: `sandbox_image.docker_ok`, `image_tag`, `repo_image_tag`, `IMAGE_DIR`, `REPO_IMAGE_DIR`, `addons.docker_installed`, `Addons.refresh`.
- Produces:
  - `build_image(tag, docker, image_dir, *, on_line: Callable[[str], None] | None = None) -> int` — with `on_line`, output is piped and each line passed on; without it, today's behaviour.
  - `class SandboxBuilder(data_dir: Path, *, docker=DOCKER, run_build=build_image, record=…)`: `state() -> {"state": "idle" | "building" | "done" | "failed", "step": "sandbox" | "repo" | None, "reason": "docker_missing" | "docker_stopped" | "build_failed" | None}`; `start() -> dict` (409 `busy` while building; checks Docker first); runs both builds in one thread; writes the last 40 lines to `<data>/logs/sandbox-build.log`; refreshes the `analysis` add-on when done; `async shutdown()` (kills the `docker build` child).
  - `POST /api/addons/analysis/build {step_up_token}` (step-up `settings`, audited) → 202 `{build}`; `GET /api/addons/analysis/build` → `{build}`. `POST /api/setup/sandbox/build {secret}` and `POST /api/setup/sandbox/status {secret}`.
  - `cmd_sandbox_build` keeps its console output by passing `on_line=print`.
  - `SandboxBuild({ api })` — shows Docker missing ("Get Docker Desktop", official page `https://www.docker.com/products/docker-desktop/`, then "Check again"), Docker stopped ("Start Docker Desktop, then Check again"), Build (≈ "This takes a few minutes the first time"), progress ("Building the sandbox, step 1 of 2"), done, or failed with Retry. The Add-ons card's `build_image` and `start_docker` actions render it.

- [ ] **Step 1: Write the failing tests**: `test_build_runs_both_images_in_order`, `test_docker_missing_and_stopped_are_reported_before_building`, `test_failed_build_keeps_40_lines_in_the_log_only`, `test_second_start_is_busy`, `test_shutdown_kills_the_build`, `test_api_build_needs_settings_step_up`, `test_setup_build_uses_the_secret`, `test_cli_sandbox_build_still_prints`; web `Analysis card offers Build when the image is missing`, `Docker missing links the official page`, `failed build offers Retry`, copy through `forbiddenIn`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** the named tests — PASS.
- [ ] **Step 5: Commit** `feat(analysis): build the sandbox from Settings and from setup`.

---

### Task 5: The wizard

**Files:**
- Modify: `web/src/views/SetupPage.tsx`, `web/src/api/setup.ts`, `web/src/api/models.ts`, `web/src/components/ModelsPanel.tsx`, `server/src/hearthkeep/api/routes_setup.py` (`POST /api/setup/addons` → the add-on list, same shape as `GET /api/addons`)
- Create: `web/src/views/setup/Welcome.tsx`, `YourPc.tsx`, `ChatModel.tsx`, `AddOns.tsx`, `Downloads.tsx`, `Passkey.tsx`, `Done.tsx`, `web/src/components/Stepper.tsx`
- Test: `web/src/__tests__/setup.test.tsx` (extend), `web/src/__tests__/models.test.tsx` (adapter change), `server/tests/test_setup_routes.py` (addons route)

**Interfaces:**
- Consumes: Tasks 1–4 routes; `DownloadList`, the model rows and confirm box from `ModelsPanel`; `StartupSwitch`; `SandboxBuild`; 2a secret handling (`readSetupSecret`, the `hashchange` listener, `waitForSetupDone`, `patiently`).
- Produces:
  - `type ModelsApi = { getCatalog(); startInstall(items, makeDefault); getJob(); cancel(); retry(); switchLocal(card); switchExternal(baseUrl, apiKey, model) }`; `sessionModelsApi` (today's functions, step-up inside) and `setupModelsApi(secret)` (setup routes). `ModelsPanel` takes `api: ModelsApi` (Settings passes `sessionModelsApi`) and exports `ChatModelPicker` and `ImageModelPicker` for the wizard. Settings' behaviour and tests stay the same.
  - `Stepper({ steps, current })` — an ordered list with `aria-current="step"`, readable at phone width.
  - Steps (each can be skipped or returned to except Passkey; "Back" and "Next" or "Skip for now"):
    1. **Welcome:** what Hearthkeep is, what setup will do, "about 10 to 30 minutes, mostly downloading" and the disk the recommended chat model plus engine needs (from the catalog).
    2. **Your PC:** GPU and graphics memory (or "No NVIDIA graphics card"), memory, free space, the data folder with "Change…" (a text field prefilled with the current folder, the 2a sentence on error, a "Moving…" state that waits for `/api/health` and re-checks status). Hidden "Change…" when `can_move_data` is false.
    3. **Chat model:** `ChatModelPicker` with the recommendation, alternatives with size and licence link, and "Use a model server I already run". Choosing adds to the download list; nothing downloads yet.
    4. **Add-ons:** one card each for Analysis (`SandboxBuild`), Images (`ImageModelPicker`, hidden reasons as in 2b), GitHub ("Add your GitHub token in Settings › GitHub after setup"), Phone and devices ("You can add your phone later, from Settings."), Start at login (`StartupSwitch`, on by default).
    5. **Downloads:** starts one install job for everything chosen (`make_default` true) and shows `DownloadList`; Retry and Stop; "Next" is allowed while downloads continue ("They keep going while you set up your passkey").
    6. **Passkey:** today's Create passkey step, unchanged in behaviour.
    7. **Done:** "Hearthkeep is ready" with what's set up and what's still downloading, and "Open Hearthkeep" (`page.replace('/')`).
  - The current step index is stored in `sessionStorage['hk.setup.step']` (try/catch; the secret is never stored).

- [ ] **Step 1: Write the failing tests** in `setup.test.tsx`: `walks through every step with the setup adapter` (each POST carries the secret in the body and never in a URL), `a reload keeps the step and not the secret`, `moving the data folder waits for the server and carries on`, `downloads start once with everything chosen`, `start at login is on by default and can be turned off`, `analysis build and docker-missing states render`, `next is allowed while downloads run`, `phone card points to Settings later`, `stepper marks the current step`, every string through `forbiddenIn`; and in `models.test.tsx` `settings still uses the session adapter` (existing tests pass unchanged). Server: `test_setup_addons_matches_the_session_list`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** `npm test`, `npm run build`, and `uv run pytest -q tests/test_setup_routes.py` — PASS.
- [ ] **Step 5: Commit** `feat(web): the setup wizard`.

---

### Task 6: Docs and the end-to-end check

**Files:**
- Modify: `docs/install.md` (§2 quick start through the wizard, §8 first start, §14), `README.md` (quick start, screenshots list), `docs/security.md` (setup routes and the secret standing in for step-up)

- [ ] **Step 1: Docs.** install.md describes the wizard steps in owner words; §7 (remote access before the first passkey) stays, with a note that the wizard will offer it in a later update. security.md lists the setup routes, the gate order and why the secret replaces step-up only while setup is open.
- [ ] **Step 2: Visual check without a real passkey.** Serve `web/dist` from a throwaway fake API in the session scratchpad (as in 2a) and walk the wizard at desktop and phone width: every step, a folder move, a download list, a failed build, a reload mid-way. Record findings in the report; fix anything broken in this task.
- [ ] **Step 3: Run** `uv run pytest -q`, `npm test`, `npm run build`, `python scripts/privacy_scan.py`. Expected: all pass.
- [ ] **Step 4: Commit** `docs: set up Hearthkeep with the browser wizard`.

## Release gate additions (before v0.1)

- [ ] Run Setup.cmd on a clean Windows account and finish the whole wizard with a real passkey, choosing a non-default data folder, the recommended chat model, Analysis and Start at login.
- [ ] Confirm the start-at-login task starts Hearthkeep after signing out and in.
