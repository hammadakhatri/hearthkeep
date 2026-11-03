# Hearthkeep Phase 5, Project 2a: Setup Backbone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person double-clicks `Setup.cmd`, gets uv and the web app without typing anything, creates their passkey in the browser through a secret-protected setup mode, and from then on Hearthkeep runs detached under a small launcher that the web app can stop and restart behind a passkey step-up; existing installs upgrade in place through a one-time migration and see a "Hearthkeep was updated" note.

**Architecture:** `Setup.cmd` stays thin: it installs a pinned, hash-checked uv and runs `uv run --project server hearthkeep setup`. Everything else is Python. A `Host` interface (`host.py`) holds every OS-specific act (default data folder, path rules, start at login, Start-menu shortcut, detached spawn, the data-folder locator), with Windows as the only implementation and an `UnsupportedHost` that reports "unsupported" and never acts. `hearthkeep setup` checks the data folder, makes sure the web bundle is there (verified release bundle, or a Node build on `main`), then runs the normal app in-process in **setup mode**: a one-time secret lives only in memory, the browser opens `/setup#<secret>`, and `/api/setup/*` hands over the first-device enrolment code only to a loopback request carrying the secret in its body. When the first passkey is created the setup server exits, `hearthkeep setup` starts the launcher detached, and the console says it's done. The launcher (`hearthkeep launch`, run with `pythonw`) keeps one `serve` child and restarts it when the server exits with `EXIT_RESTART`; the web app asks for that through `POST /api/server/restart|stop`. Start at login and the Start-menu shortcut both start the launcher. A one-time `upgrade.py` migration runs at the first start of an existing data folder.

**Tech Stack:** The Phase 1–4 stack (Python 3.12.6/uv, FastAPI, uvicorn, httpx, pytest, ruff; React 19 + TypeScript, Vitest). Windows `cmd`, Task Scheduler (`schtasks`) and `powershell.exe` (for the `.lnk` only). GitHub Actions for the release job. No new runtime dependency.

**Spec:** `docs/superpowers/specs/2026-11-03-hearthkeep-phase-5-design.md` §5.1, §5.2, §5.5 and §6a (this project), with §1–3, §12 and §13 for context; setup facts and audit evidence in `.superpowers/phase5-audit-notes.md` (audit 1 "Setup today"; U11 and U12 stay with 2c and 3). Builds on the merged project 1 plan `2026-11-03-hearthkeep-addons.md` (`addons.py`, `GET /api/addons`, the "no chat model" state, tool gating, Settings › Add-ons): reuse it, don't redo it. Out of scope: the catalog and downloads (2b); the wizard screens beyond the minimal `/setup` proof page, Tailscale, the start-at-login switch, advanced settings (2c); the UX pass (3); duplication cleanup (4, but add none); the docs rewrite (5, but update the lines this changes); demos (6).

## Global Constraints

- **Loopback only.** Nothing new listens anywhere. The server still binds `127.0.0.1` only. Setup endpoints also refuse any request whose TCP peer isn't `127.0.0.1` or `::1`.
- **Passkeys are the only sign-in.** Setup mode hands over a first-device enrolment code; the passkey ceremony is the existing `/api/auth/enroll/*`. No password, token or "skip" path.
- **Widening actions need step-up.** Restart and stop take a step-up token with purpose `server` and are audited (`server.restart`, `server.stop`, `server.restart_denied`, `server.stop_denied`).
- **The enrolment code and the setup secret are never in a URL query, a log or the audit log.** The secret travels only in the URL fragment and request bodies; the code only in a response body (`Cache-Control: no-store`). Neither is printed to a log file; the console prints the setup link only when the browser can't be opened. `serve --log-file` no longer writes the enrolment code.
- **Nothing personal in the repo.** No user name, user path or GitHub owner is hard-coded: the release repository is read from the clone's `origin` remote at run time. Paths in tests are temp folders or `PureWindowsPath` literals like `C:\Hearthkeep\data`.
- **No AI attribution or co-author lines in commits.** Conventional Commits messages with no trailers.
- **Existing tests keep passing.** A test that pins behaviour this project deliberately changes (the task XML arguments, the `serve` code print, the `run_server` signature) is updated in the task that changes it, and the task names it.
- **Copy rules.** "passkey", never "Face ID / Touch ID" in new copy. No CLI command (`hearthkeep …`, `uv run …`, `--data`) and no placeholder (`<data>`) in any owner-visible text, web or console. Pointing at `Setup.cmd` ("double-click Setup.cmd") and at the Start menu is allowed. Console copy is ASCII only (the Windows console code page).
- **Windows is the only host.** Every OS-specific act goes through `host.current_host()`. On any other OS, `setup`, `launch`, `open`, start at login and the shortcut report "unsupported" and never create, spawn or run anything. `serve` keeps working everywhere (developers, CI). CI stays on Linux; Windows behaviour is unit-tested through fakes.
- **The upgrade keeps existing installs working.** Today's `config.toml`, `models.toml`, cards, credentials, sessions and tool levels are read as they are. The migration never touches config files or cards and never re-enrols anyone.
- **Commands.** Server, from `server/`: `uv run pytest -q` and `uv run ruff check . ../scripts`. Web, from `web/`: `npx vitest run src/__tests__/<file>`, `npm test` and `npm run build`.

## Review Focus

1. **A second process on the PC tries to fetch the enrolment code.** Without the secret, with a wrong one, with the secret in a `?secret=` query, from a non-loopback peer, or without the CSRF header and Origin: no code is issued (no new `enrollment_codes` row), the answer is 403 (`setup_secret`, `loopback_only` or `csrf`), a guessing loop is cut off at 429, and the secret appears in no log line, console line or audit row. (Task 6: `test_setup_code_needs_the_secret_and_loopback`, `test_setup_secret_and_code_never_logged`.)
2. **Setup is run again after a passkey exists.** `Setup.cmd` a second time does not start setup mode: it says "already set up", makes sure the launcher runs and opens the app. On a setup-mode server whose passkey was just made, `/setup` redirects to `/` and every `/api/setup/*` answers 404, even with the right secret. (Task 6: `test_setup_rerun_after_passkey_opens_app_and_setup_is_gone`.)
3. **A restart or stop request without step-up.** No token, a token for another purpose, another session's token or a reused token: 403 `step_up_required`, an audited `server.restart_denied`/`server.stop_denied`, and the server keeps running (no exit requested). Unsupervised, restart is 409 `not_supervised` and the token isn't consumed. (Task 4: `test_restart_and_stop_need_step_up`.)
4. **The migration runs twice** (two starts, or a start after a crash half-way). The second run changes nothing: no second `schtasks /Create`, no second shortcut, the same rows, the same note, no new audit rows. A step that failed the first time is retried, and its note item isn't duplicated. (Task 8: `test_upgrade_twice_changes_nothing_the_second_time`, `test_failed_step_retries_next_start`.)
5. **An unusual host: a long or unusual data path, or not Windows.** A data path over 100 characters, a network path, non-ASCII letters, `%`, `!`, `&` or quotes, a folder inside the repo, or a non-empty unrelated folder is refused with a plain sentence before anything is created; a path with spaces works end to end (task XML, shortcut, launcher argv). On macOS or Linux, setup, launch, open, start at login and the shortcut say "unsupported" and never act. (Task 1: `test_unusual_data_paths`, `test_other_os_reports_unsupported_and_never_acts`; Task 6: `test_setup_on_other_os_creates_nothing`.)

## Calls made in this plan

The spec leaves these open. Check them while reading:

- **Supervision: a small launcher process, exit codes as the only channel.** `hearthkeep launch` (under `pythonw`) runs `serve` as a child. Exit `0` (stop) and `2` (config error) end the launcher; `75` (`EXIT_RESTART`) starts a new child; anything else is a crash, restarted after 5 s, at most 3 times in 10 minutes. No IPC port, no extra listener. A lock file makes it single-instance. Task Scheduler is used only for start at login, as today.
- **Setup runs the server in the console process, then hands over.** The setup-mode server lives in the `Setup.cmd` window (so closing the window ends the secret, as §5.2 says). When the first passkey is made, it exits and `hearthkeep setup` starts the launcher detached; the setup page waits for `/api/health` to report `setup_mode: false` and opens `/`. The session cookie carries over (same database).
- **The setup secret** is `secrets.token_urlsafe(32)`, held in `SetupMode` in memory, compared with `hmac.compare_digest`, cleared when the first passkey is created. Endpoints are POST so it rides in the body under the existing CSRF checks. Each `enrollment-code` call issues a fresh 15-minute code (rate-limited with the secret check). Threat covered: other users and services on the PC, other browser tabs and websites (Host and Origin checks), and anything that can't read the console window or the browser. A process running as the same user can already read both; that stays out of scope.
- **Release version and bundle.** The version is `hearthkeep.__version__` (equal to `server/pyproject.toml`'s, pinned by a test). A *release checkout* is one whose version matches `^\d+\.\d+\.\d+$` and isn't `0.0.0`; `main` carries `0.0.0` or a `.devN` version. The release workflow attaches two assets: `hearthkeep-<v>.zip` (the tag's source plus the prebuilt `web/dist` with its manifest) and `hearthkeep-web-<v>.zip` (the bundle alone, for people who cloned the tag), each with a `.sha256`. Setup accepts `web/dist` when its `.bundle.json` manifest names this version and every file's SHA-256 matches; otherwise, on a tag clone, it downloads `hearthkeep-web-<v>.zip` and its `.sha256` from the GitHub release of the clone's `origin` remote (HTTPS only), checks the SHA-256, extracts it safely and writes the manifest. The release zip's manifest is the checksum for people who downloaded the zip; the release page's `.sha256` lets them check the zip itself.
- **`main` without Node but with an existing `web/dist`** uses that build as it is, with a line saying so. Only `main` with neither Node nor a build stops with "main is for developers".
- **uv install.** `Setup.cmd` prefers `%USERPROFILE%\.local\bin\uv.exe`, then any `uv` on PATH, else downloads `https://astral.sh/uv/<UV_VERSION>/install.ps1`, checks its SHA-256 against the pinned value with `Get-FileHash`, and runs it with `UV_INSTALL_DIR=%USERPROFILE%\.local\bin` and `UV_NO_MODIFY_PATH=1` (it doesn't change the user's PATH; Setup.cmd calls uv by full path).
- **The data folder is recorded twice, on purpose.** The start-at-login task and the shortcut carry `--data "<folder>"` in their own arguments (written by code; the owner never types them), so `startup.status` keeps reading the folder from the task as today. `%LOCALAPPDATA%\Hearthkeep\location.json` records it too, for `Setup.cmd` re-runs and for `launch`/`open` without `--data`. The migration writes the locator only when it's missing or names a folder that no longer exists, so two data folders on one PC don't fight.
- **Data folder rules (Windows):** absolute with a drive letter, not a network path, at most 100 characters (llama.cpp's 260 limit minus the deepest path Hearthkeep makes under it), each part only `A–Z a–z 0–9 space _ - . ( )`, not inside the Hearthkeep folder, and either missing, empty, or already a Hearthkeep data folder (`config.toml` or `hearthkeep.db`). 2a uses the default `C:\Hearthkeep\data`; choosing another is a 2c wizard screen, which reuses `check_data_dir`.
- **Shortcut.** `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Hearthkeep.lnk`, made by `powershell.exe` from System32 with a fixed script and the values passed in environment variables (no quoting into code). It runs `pythonw -m hearthkeep open --data "<folder>"`, which starts the launcher if `/api/health` doesn't answer and opens `public_origin`. It uses pythonw's icon; a Hearthkeep icon is project 3.
- **Restart needed.** `ServerControl.restart_needed` is a set of reason ids that later projects add to (`mark_restart_needed`). 2a exposes it in `GET /api/server` and shows "Restart now" in Settings › Add-ons and the new Settings › Hearthkeep section; nothing in 2a sets it except tests.
- **Stop** also needs step-up (§5.5 says both are behind step-up), even though stopping narrows: it cuts off every device until someone is at the PC.
- **The "Hearthkeep was updated" note** is stored only for an existing install (a credential or a tool-level row existed before the migration), shown until the owner chooses "Got it", and lists stored items plus, at read time, "The analysis sandbox needs rebuilding" when Analysis is `needs_image`. Its button leads to the add-on's card (building from the app arrives with 2b/2c). With nothing to do it says so.
- **Placeholder level rows** (`wallet`, `news`, `demo`) are dropped only when the key isn't in `DEFAULT_LEVELS`, so a future tool that reuses the name keeps its row (and the tests' `demo` default keeps its row unless a test opts into `real_levels`).
- **`/setup` without setup mode** redirects to `/` (302); `/api/setup/*` is 404. `GET /api/health` gains `setup_mode` (it already reveals "no passkey" through `login/options`).
- **`serve` prints the enrolment code only to an interactive console** without `--log-file`. With a log file (start at login, the launcher) it prints "No passkey is set up yet. To finish setting up, double-click Setup.cmd in the Hearthkeep folder."

---

## File structure

```
Setup.cmd                              # NEW: thin entry point (CRLF, ASCII)
.gitattributes                         # NEW or MODIFIED: *.cmd text eol=crlf
.gitignore                             # release zips, .cache/, web/dist.new|old
.github/workflows/release.yml          # NEW: tag → build web bundle, draft release with assets
scripts/build_release.py               # NEW: the two zips, manifest and checksums
server/src/hearthkeep/
  host.py           # NEW: Host interface, WindowsHost, UnsupportedHost, data-folder rules, locator
  shortcut.py       # NEW: Start-menu .lnk through powershell (Windows)
  lifecycle.py      # NEW: ServerControl, exit codes
  launcher.py       # NEW: launch loop, lock, open_app, health probe
  setupmode.py      # NEW: SetupMode, setup_url
  webbundle.py      # NEW: release version, manifest, download+verify, Node build
  upgrade.py        # NEW: the one-time migration and the update note
  api/routes_setup.py   # NEW: /api/setup/status, /api/setup/enrollment-code
  api/routes_server.py  # NEW: /api/server, restart, stop; /api/update-note (Task 8)
  procutil.py       # detached_popen_kwargs
  startup.py        # task runs the launcher; StartupStatus.launcher; pythonw_path
  cli.py            # build_app, run_server → int, setup, launch, open; code print rule
  db.py             # migration 10: app_state
  api/app.py        # control, setup, /setup route, setup_mode in health, WEB_DIST
  api/common.py     # Services.control, Services.setup
  api/routes_auth.py  # StepUpPurpose "server"; setup completes on first passkey
web/src/
  api/setup.ts, api/server.ts                     # NEW
  views/SetupPage.tsx                             # NEW: minimal proof page
  components/ServerPanel.tsx, RestartNotice.tsx, UpdateNote.tsx   # NEW
  App.tsx, api/webauthn.ts, shell/AppShell.tsx, shell/AddonsContext.tsx,
  views/Settings.tsx, components/AddonsPanel.tsx
docs: README.md (quick start), docs/install.md (§2, §8, §10, §14), docs/security.md:22, CONTRIBUTING.md (Releasing)
```

---

### Task 1: The `Host` interface, data-folder rules and the locator

**Files:**
- Create: `server/src/hearthkeep/host.py`
- Modify: `server/src/hearthkeep/procutil.py` (`detached_popen_kwargs`), `server/src/hearthkeep/startup.py` (`pythonw_path`, used by `enable`)
- Test: `server/tests/test_host.py`

**Interfaces:**
- Produces (`procutil.py`): `detached_popen_kwargs() -> dict`. Windows: `{"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, "stdin": DEVNULL, "stdout": DEVNULL, "stderr": DEVNULL, "close_fds": True}`; elsewhere `{"start_new_session": True, ...the same three DEVNULLs, "close_fds": True}`. Flags read with `getattr(subprocess, name, 0)` so the module imports anywhere.
- Produces (`startup.py`): `pythonw_path() -> Path` = `Path(sys.executable).with_name("pythonw.exe")`; `enable` uses it as its default (behaviour unchanged).
- Produces (`host.py`):
  - `REPO_ROOT: Path = Path(__file__).resolve().parents[3]`.
  - `DEFAULT_DATA_DIR = PureWindowsPath(r"C:\Hearthkeep\data")`; `MAX_DATA_DIR_CHARS = 100`; `LOCATION_FILE = "location.json"`.
  - `UNSUPPORTED_HOST = "Setting up Hearthkeep and running it in the background is built in only on Windows for now."`
  - `class HostUnsupported(Exception)` (message `UNSUPPORTED_HOST`).
  - `check_data_dir_text(text: str) -> str | None`: the pure Windows rules on `PureWindowsPath(text)`, so it runs on any OS. Returns the first failing message:
    - not absolute with a drive, or UNC (`\\`): `"Choose a folder on this PC's own drive, like C:\Hearthkeep\data."`
    - longer than `MAX_DATA_DIR_CHARS`: `"That folder path is too long. Choose one of 100 characters or fewer, like C:\Hearthkeep\data."`
    - a part outside `[A-Za-z0-9 _.()-]+`, or a part ending in a space or dot: `"Use only English letters, numbers, spaces and - _ . ( ) in the folder path."`
  - `check_data_dir(path: Path, *, repo_root: Path = REPO_ROOT) -> str | None`: `check_data_dir_text(str(path))`, then: inside `repo_root` (resolved, case-insensitive) → `"Keep Hearthkeep's data outside the Hearthkeep program folder."`; an existing non-empty folder with neither `config.toml` nor `hearthkeep.db` → `"That folder already has other files in it. Choose an empty folder."`; an existing file → the same message.
  - `class Host(Protocol)`: `name: str`; `supported: bool`; `default_data_dir() -> Path | None`; `check_data_dir(path: Path) -> str | None`; `startup_status(*, run=subprocess.run) -> StartupStatus`; `enable_startup(data_dir: Path, *, run=subprocess.run) -> None`; `disable_startup(*, run=subprocess.run) -> bool`; `install_shortcut(data_dir: Path, *, run=subprocess.run) -> None`; `spawn_detached(argv: list[str], *, popen=subprocess.Popen) -> None`; `read_location() -> Path | None`; `write_location(data_dir: Path) -> None`.
  - `class WindowsHost`: `name = "windows"`, `supported = True`. Startup methods delegate to `startup.status/enable/disable`. `install_shortcut` delegates to `shortcut.install_shortcut` (Task 2 creates it; until then it raises `NotImplementedError`). `spawn_detached` calls `popen(argv, **detached_popen_kwargs())`. The locator is `Path(os.environ["LOCALAPPDATA"]) / "Hearthkeep" / LOCATION_FILE`, JSON `{"data_dir": "<resolved path>"}`, written atomically (temp file + `os.replace`); a missing or unreadable file reads as None.
  - `class UnsupportedHost`: `name = "unsupported"`, `supported = False`; `default_data_dir()` None; `check_data_dir` returns `UNSUPPORTED_HOST`; `startup_status` returns `StartupStatus(False, False, None)` without running anything; `enable_startup`, `install_shortcut`, `spawn_detached` and `write_location` raise `HostUnsupported`; `disable_startup` returns False; `read_location` None.
  - `current_host() -> Host`: `WindowsHost()` when `startup.is_windows()`, else `UnsupportedHost()`. Everything later asks this, never `sys.platform`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("text, ok", [
    (r"C:\Hearthkeep\data", True),
    (r"D:\My Hearthkeep\data (2)", True),
    ("C:\\" + "a" * 98, False),                         # 101 characters
    (r"\\nas\share\hearthkeep", False),
    (r"hearthkeep\data", False),                         # relative
    (r"C:\Dåta\Hearthkeep", False),                     # non-ASCII
    (r"C:\Hearth%PATH%\data", False), (r"C:\Hearth!\data", False),
    (r"C:\A&B\data", False), ('C:\\Hearth"keep', False), (r"C:\data. \x", False),
])
def test_unusual_data_paths(text, ok):
    message = check_data_dir_text(text)
    assert (message is None) == ok
    if message:
        assert message.isascii() and "<" not in message and "hearthkeep " not in message.lower()

def test_check_data_dir_refuses_repo_and_foreign_folders(tmp_path):
    repo = tmp_path / "repo"; (repo / "server").mkdir(parents=True)
    assert check_data_dir(repo / "data", repo_root=repo) == "Keep Hearthkeep's data outside the Hearthkeep program folder."
    foreign = tmp_path / "photos"; foreign.mkdir(); (foreign / "a.jpg").write_bytes(b"x")
    assert "other files" in check_data_dir(foreign, repo_root=repo)
    mine = tmp_path / "hk"; mine.mkdir(); (mine / "config.toml").write_text("")
    assert check_data_dir(mine, repo_root=repo) is None          # an existing data folder is fine
# (on Linux tmp_path isn't a drive path: these two tests monkeypatch check_data_dir_text to return None)

def test_other_os_reports_unsupported_and_never_acts(monkeypatch):
    monkeypatch.setattr(startup, "is_windows", lambda: False)
    host = current_host()
    assert host.supported is False and host.default_data_dir() is None
    def forbidden(*a, **k): raise AssertionError("ran something")
    assert host.startup_status(run=forbidden) == StartupStatus(False, False, None)
    for act in (lambda: host.enable_startup(Path("x"), run=forbidden),
                lambda: host.install_shortcut(Path("x"), run=forbidden),
                lambda: host.spawn_detached(["x"], popen=forbidden),
                lambda: host.write_location(Path("x"))):
        with pytest.raises(HostUnsupported):
            act()
    assert host.disable_startup(run=forbidden) is False

def test_windows_host_spawns_detached(monkeypatch):      # popen fake receives detached_popen_kwargs()
def test_location_round_trip_and_bad_file(tmp_path, monkeypatch):   # LOCALAPPDATA=tmp; garbage JSON → None
def test_detached_kwargs_on_each_os(monkeypatch)          # creationflags on win32, start_new_session elsewhere
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_host.py -q`
Expected: FAIL (`ModuleNotFoundError: hearthkeep.host`)

- [ ] **Step 3: Implement `procutil.detached_popen_kwargs`, `startup.pythonw_path` and `host.py`**

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check . ../scripts`
Expected: PASS, no lint findings

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(host): one host interface for setup and lifecycle, with data-folder rules"
```

---

### Task 2: Start at login runs the launcher, and the Start-menu shortcut

**Files:**
- Create: `server/src/hearthkeep/shortcut.py`
- Modify: `server/src/hearthkeep/startup.py` (`task_xml` arguments, `StartupStatus.launcher`, `_parse`, module docstring), `server/src/hearthkeep/host.py` (`WindowsHost.install_shortcut`), `server/tests/test_startup.py` (the argument assertions of `test_task_xml_has_logon_trigger_no_window_no_time_limit` and the quoting test), `server/tests/test_cli.py` (`test_cli_startup_commands` if it asserts `serve`), `docs/install.md` §10 (the task now starts Hearthkeep's launcher, which restarts it if it stops; logs in `logs\hearthkeep.log` and `logs\launcher.log`) and the §10 "make the task by hand" arguments
- Test: `server/tests/test_shortcut.py`, `server/tests/test_startup.py`

**Interfaces:**
- Consumes: Task 1 `pythonw_path`, `Host`.
- Produces (`startup.py`):
  - `task_xml(...)` Arguments become `-m hearthkeep launch --data "<data>"` (`quote_arg` as today; no `--log-file`: the launcher passes it to `serve`). `_DATA_ARG` still finds the folder.
  - `StartupStatus.launcher: bool = False` (last field, default keeps existing constructors valid); `_parse` sets it when the Arguments match `r"-m\s+hearthkeep\s+launch\b"`. A task made by today's code reads `launcher=False`.
- Produces (`shortcut.py`):
  - `SHORTCUT_NAME = "Hearthkeep.lnk"`; `shortcut_path() -> Path` = `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Hearthkeep.lnk`; `powershell_exe() -> str` = `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe` (raises `ShortcutError` when SystemRoot is unset, like `schtasks_exe`).
  - `class ShortcutError(Exception)` (owner message).
  - `SHORTCUT_SCRIPT` (exact, fixed): `$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:HK_LNK); $s.TargetPath = $env:HK_TARGET; $s.Arguments = $env:HK_ARGS; $s.WorkingDirectory = $env:HK_DIR; $s.Description = 'Open Hearthkeep'; $s.Save()`
  - `install_shortcut(data_dir: Path, *, run=subprocess.run, pythonw: Path | None = None) -> None`: runs `[powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", SHORTCUT_SCRIPT]` with `env={**os.environ, "HK_LNK": str(shortcut_path()), "HK_TARGET": str(pythonw or pythonw_path()), "HK_ARGS": f"-m hearthkeep open --data {quote_arg(str(data))}", "HK_DIR": str(data)}`, `capture_output=True`, `timeout=30`, `creationflags=NO_WINDOW`. Non-zero, timeout or `OSError` → `ShortcutError("The Start menu shortcut couldn't be made.")` (the detail logged by type only). Overwrites an existing shortcut (idempotent).

- [ ] **Step 1: Write the failing tests**

```python
def test_task_runs_the_launcher_with_the_data_folder(tmp_path):
    xml = task_xml(pythonw=Path(r"C:\py\pythonw.exe"), data_dir=Path(r"D:\My Hearthkeep\data"), user="PC\\me")
    args = parse(xml).findtext("t:Actions/t:Exec/t:Arguments", namespaces=NS)
    assert args == '-m hearthkeep launch --data "D:\\My Hearthkeep\\data"'
    status = _parse(xml)
    assert status.launcher and status.data_dir == r"D:\My Hearthkeep\data"
def test_todays_task_reads_as_not_launcher():             # fixtures/upgrade/old-task.xml (Task 8 adds it; inline today's XML here)
def test_shortcut_passes_values_by_environment(tmp_path, monkeypatch):
    seen = {}
    def run(argv, **kw):
        seen.update(argv=argv, env=kw["env"])
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    install_shortcut(Path(r"C:\Hearth keep\data"), run=run, pythonw=Path(r"C:\py\pythonw.exe"))
    assert seen["argv"][-1] == SHORTCUT_SCRIPT and seen["argv"][0].endswith(r"System32\WindowsPowerShell\v1.0\powershell.exe")
    assert seen["env"]["HK_ARGS"] == '-m hearthkeep open --data "C:\\Hearth keep\\data"'
    assert seen["env"]["HK_LNK"].endswith(r"Start Menu\Programs\Hearthkeep.lnk")
def test_shortcut_failure_is_a_plain_error(tmp_path)       # exit 1, TimeoutExpired, OSError → ShortcutError, no path in message
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_startup.py tests/test_shortcut.py -q`
Expected: FAIL (arguments still say `serve`; no `hearthkeep.shortcut`)

- [ ] **Step 3: Implement the task change, `StartupStatus.launcher`, `shortcut.py` and `WindowsHost.install_shortcut`; update the startup tests that pinned `serve` and the install.md §10 lines**

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check . ../scripts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server docs
git commit -m "feat(startup): start at login and a Start menu shortcut both start the launcher"
```

---

### Task 3: `ServerControl`, the launcher and `hearthkeep open`

**Files:**
- Create: `server/src/hearthkeep/lifecycle.py`, `server/src/hearthkeep/launcher.py`
- Modify: `server/src/hearthkeep/cli.py` (`run_server` returns an exit code and takes a control; `build_app` split out of `cmd_serve`; `cmd_serve` returns the control's code and follows the code-print rule; new `launch` and `open` commands; module docstring), `server/tests/test_cli.py` (the `serve` fixture's `_run_server(self, app, host, port, **kw)`; the code-print tests), `docs/install.md:408` (no code in the log; "double-click Setup.cmd" instead)
- Test: `server/tests/test_launcher.py`, `server/tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1 `current_host`, `Host.read_location`, `Host.spawn_detached`, `pythonw_path`; `startup.log_path`.
- Produces (`lifecycle.py`):
  - `EXIT_STOPPED = 0`, `EXIT_RESTART = 75`, `SUPERVISED_ENV = "HEARTHKEEP_SUPERVISED"`.
  - `class ServerControl`: `__init__(self, *, supervised: bool, setup_mode: bool = False, clock: Callable[[], datetime] = utcnow)`; attributes `supervised`, `setup_mode`, `started_at: datetime`, `exit_code: int | None`, `restart_needed: set[str]`; `attach(shutdown: Callable[[], None]) -> None`; `request(exit_code: int) -> None` (the first request wins: sets `exit_code` and calls `shutdown` once; later requests are ignored; before `attach` it only records); `mark_restart_needed(reason: str) -> None`.
- Produces (`cli.py`):
  - `run_server(app, host: str, port: int, *, control: ServerControl | None = None, on_started: Callable[[], None] | None = None) -> int`: builds `uvicorn.Config` with today's options and a `uvicorn.Server`, attaches `lambda: setattr(server, "should_exit", True)` to the control, and when `on_started` is given starts a daemon thread that waits (0.1 s steps, up to 30 s) for `server.started` and calls it once. Returns `control.exit_code` if set, else 0.
  - `build_app(settings: Settings, *, verifier, control: ServerControl, setup: SetupMode | None = None, make_llama=…, make_comfy=…, detect_vram=…) -> tuple[FastAPI, list[str]]`: today's `cmd_serve` body from `SecretStore` to `create_app`, passing `control` and `setup` (Task 4 adds the parameters to `create_app`; until then `build_app` doesn't pass them). Raises `ProcessError` as today.
  - `cmd_serve`: `control = ServerControl(supervised=os.environ.get(SUPERVISED_ENV) == "1")`; returns `run_server(...)`. The first-device code is printed with `_print_code` only when `args.log_file` is unset and `sys.stdout.isatty()`; otherwise it prints `NO_PASSKEY_YET = "No passkey is set up yet. To finish setting up, double-click Setup.cmd in the Hearthkeep folder."` and issues no code.
  - `cmd_launch(args, *, host=None, popen=subprocess.Popen) -> int` and `cmd_open(args, *, host=None, open_url=webbrowser.open) -> int`. Both: unsupported host → print `UNSUPPORTED_HOST`, return `EXIT_USAGE`; data from `--data` (optional here) else `host.read_location()`; none → print `NOT_SET_UP = "Hearthkeep isn't set up on this PC yet. Double-click Setup.cmd in the Hearthkeep folder."`, return 1.
  - Parser: `launch` (`--data`, help "keep Hearthkeep running in the background (used by start at login)") and `open` (`--data` optional, help "open Hearthkeep in the browser, starting it if needed").
- Produces (`launcher.py`):
  - `LAUNCHER_LOG = Path("logs") / "launcher.log"`; `LOCK_FILE = Path("runtime") / "launcher.lock"`; `CRASH_LIMIT = 3`; `CRASH_WINDOW_S = 600.0`; `CRASH_PAUSE_S = 5.0`; `HEALTH_TIMEOUT_S = 1.0`; `START_WAIT_S = 60.0`.
  - `serve_argv(data_dir: Path) -> list[str]` = `[sys.executable, "-m", "hearthkeep", "serve", "--data", str(data_dir), "--log-file", str(startup.log_path(data_dir))]`.
  - `launcher_argv(data_dir: Path) -> list[str]` = `[str(pythonw_path()), "-m", "hearthkeep", "launch", "--data", str(data_dir)]`.
  - `acquire_lock(path: Path) -> object | None`: an exclusive non-blocking lock on an open file (`msvcrt.locking(fd, LK_NBLCK, 1)` on Windows, `fcntl.flock(LOCK_EX | LOCK_NB)` elsewhere); None when another launcher holds it. The returned handle keeps the file open.
  - `run_launcher(data_dir: Path, *, popen=subprocess.Popen, sleep=time.sleep, clock=time.monotonic, lock=acquire_lock) -> int`: no lock → 0 at once. Loop: start `serve_argv` with `env={**os.environ, SUPERVISED_ENV: "1"}` and `creationflags=NO_WINDOW`; `wait()`; `EXIT_RESTART` → again; `0` or `EXIT_USAGE` → return that; anything else → a crash: crashes older than `CRASH_WINDOW_S` are forgotten, the `CRASH_LIMIT`-th within the window returns 1, otherwise `sleep(CRASH_PAUSE_S)` and again. Each start and exit is one line in `launcher.log` (time, event, exit code; never a path or argument).
  - `health(base_url: str, *, timeout: float = HEALTH_TIMEOUT_S) -> dict | None`: `GET {base}/api/health` with `httpx`, `trust_env=False`; a 200 JSON with `status == "ok"` and a `setup_mode` key, else None. Never raises.
  - `wait_for_health(base_url, timeout_s: float = START_WAIT_S, *, probe=health, sleep=time.sleep, clock=time.monotonic, want_setup_mode: bool | None = None) -> bool`.
  - `open_app(settings: Settings, host: Host, *, probe=health, open_url=webbrowser.open, wait=wait_for_health) -> int`: base `http://127.0.0.1:{settings.port}`; if `probe(base)` is None, `host.spawn_detached(launcher_argv(settings.data_dir))` and `wait(base)`; then `open_url(settings.public_origin)` whatever happened (the app shows "Can't reach Hearthkeep" with Retry if it's still starting). Returns 0.

- [ ] **Step 1: Write the failing tests**

```python
def test_restart_exit_starts_a_new_child_and_stop_ends(tmp_path):
    codes = iter([EXIT_RESTART, EXIT_RESTART, 0])
    popen = FakePopen(lambda: next(codes))
    assert run_launcher(tmp_path, popen=popen, sleep=no_sleep, lock=always_lock) == 0
    assert len(popen.started) == 3
    assert all(c.env[SUPERVISED_ENV] == "1" and c.argv[3:5] == ["serve", "--data"] for c in popen.started)
def test_config_error_ends_the_launcher(tmp_path)         # child exits 2 → returns 2 after one start
def test_crash_loop_gives_up_after_three_in_ten_minutes(tmp_path)   # fake clock; 3 crashes in 9 min → 1; spaced 11 min apart → keeps going
def test_second_launcher_exits_at_once(tmp_path):         # real acquire_lock twice on the same file → second None; run_launcher → 0, popen never called
def test_launcher_log_has_no_paths(tmp_path)              # str(tmp_path) not in launcher.log
def test_server_control_first_request_wins():
    calls = []
    c = ServerControl(supervised=True); c.attach(lambda: calls.append(1))
    c.request(EXIT_RESTART); c.request(EXIT_STOPPED)
    assert c.exit_code == EXIT_RESTART and calls == [1]
def test_open_starts_the_launcher_only_when_not_answering(tmp_path)   # probe None → spawn launcher_argv + wait; probe ok → no spawn; open_url(public_origin) both times
def test_open_and_launch_without_a_location_say_not_set_up(tmp_path, monkeypatch, capsys)
def test_open_and_launch_on_other_os_are_unsupported(monkeypatch, capsys)   # EXIT_USAGE, nothing spawned
def test_serve_returns_the_requested_exit_code(serve)      # fake run_server returns 75 → cmd_serve 75
def test_serve_with_log_file_never_writes_the_code(serve, tmp_path):
    assert serve("--log-file", str(tmp_path / "out.log")) == 0
    text = (tmp_path / "out.log").read_text(encoding="utf-8")
    assert NO_PASSKEY_YET in text and "enrollment code" not in text.lower()
    assert serve.query("SELECT COUNT(*) FROM enrollment_codes") == [(0,)]
def test_serve_prints_the_code_on_an_interactive_console(serve, monkeypatch, capsys)   # isatty True → "First-device enrollment code" as today
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_launcher.py tests/test_cli.py -q`
Expected: FAIL (`ModuleNotFoundError: hearthkeep.lifecycle`)

- [ ] **Step 3: Implement `lifecycle.py`, `launcher.py`, `run_server`, `build_app`, the `serve` code rule and the `launch`/`open` commands; update the `serve` fixture and the install.md line**

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check . ../scripts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server docs
git commit -m "feat(lifecycle): a launcher keeps Hearthkeep running and restarts it on request"
```

---

### Task 4: Stop and restart from the web app, behind step-up

**Files:**
- Create: `server/src/hearthkeep/api/routes_server.py`
- Modify: `server/src/hearthkeep/api/app.py` (`create_app(..., control: ServerControl | None = None)`; `/api/health` gains `setup_mode`; include the router), `server/src/hearthkeep/api/common.py` (`Services.control`), `server/src/hearthkeep/api/routes_auth.py` (`StepUpPurpose` gains `"server"`), `server/src/hearthkeep/cli.py` (`build_app` passes `control`), `server/tests/test_api.py` (the harness gains `control=None`; the unauthenticated-routes list gains the new routes)
- Test: `server/tests/test_api_server.py`

**Interfaces:**
- Consumes: Task 3 `ServerControl`, `EXIT_RESTART`, `EXIT_STOPPED`.
- Produces (`common.py`): `Services.control: ServerControl | None = None`. `create_app` uses `control or ServerControl(supervised=False)`.
- Produces (`app.py`): `/api/health` adds `"setup_mode": hk.control.setup_mode` (Task 6 makes it true in setup mode).
- Produces (`routes_server.py`):
  - `class StepUpTokenBody(BaseModel): step_up_token: str = Field(max_length=256)`.
  - `GET /api/server` (session) → `{"version": hearthkeep.__version__, "supervised": bool, "setup_mode": bool, "started_at": iso8601, "restart_needed": bool, "restart_reasons": sorted list}`.
  - `POST /api/server/restart` (session, body `StepUpTokenBody`): not supervised → 409 `not_supervised`, checked before the token, which stays unused. `hk.step_up.check(token, session.id, None, "server")` false → audit `server.restart_denied` `{"reason": "step_up_required"}`, 403 `step_up_required`. Else audit `server.restart` `{"reasons": sorted(restart_needed)}`, answer 202 `{"ok": true}` with `BackgroundTask(hk.control.request, EXIT_RESTART)` so the answer leaves before the server stops.
  - `POST /api/server/stop`: the same without the supervised check; events `server.stop_denied` / `server.stop`; exit code `EXIT_STOPPED`.

- [ ] **Step 1: Write the failing tests**

```python
def test_server_info_needs_a_session(harness)               # 401 for GET; POSTs 401; no X-Hearthkeep → 403 csrf
def test_server_info(harness):
    control = ServerControl(supervised=True); control.mark_restart_needed("chat_model")
    body = signed_in(harness, control=control).get("/api/server").json()
    assert body | {"started_at": None} == {"version": hearthkeep.__version__, "supervised": True, "setup_mode": False,
                                         "started_at": None, "restart_needed": True, "restart_reasons": ["chat_model"]}
@pytest.mark.parametrize("action", ["restart", "stop"])
def test_restart_and_stop_need_step_up(harness, action):
    control = ServerControl(supervised=True); control.attach(lambda: None)
    client = signed_in(harness, control=control)
    used = step_up(client, "server")
    assert client.post(f"/api/server/{action}", json={"step_up_token": used}, headers=CSRF).status_code == 202
    control.exit_code = None                              # as if it came back
    for body in ({}, {"step_up_token": "nope"}, {"step_up_token": step_up(client, "settings")},
                 {"step_up_token": used}, {"step_up_token": other_session_token(harness)}):
        r = client.post(f"/api/server/{action}", json=body, headers=CSRF)
        assert r.status_code in (403, 422) and control.exit_code is None
    assert [e["event"] for e in harness.events()].count(f"server.{action}_denied") == 4
def test_restart_unsupervised_is_409_and_keeps_the_token(harness):
    client = signed_in(harness, control=ServerControl(supervised=False))
    token = step_up(client, "server")
    assert client.post("/api/server/restart", json={"step_up_token": token}, headers=CSRF).json()["reason"] == "not_supervised"
    assert client.post("/api/server/stop", json={"step_up_token": token}, headers=CSRF).status_code == 202
def test_restart_with_step_up_answers_then_exits(harness):  # 202 body {"ok": true}; then control.exit_code == EXIT_RESTART; audit server.restart
def test_health_reports_setup_mode(harness)                  # false by default; true with ServerControl(setup_mode=True)
```

`other_session_token(harness)` enrols a second device (`cred-2`, an `add_device` code from the first session), signs it in on a second client, and returns its `server` step-up token. The harness's `start` gains `control=None`, passed to `create_app`.

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_api_server.py -q`
Expected: FAIL (404 for `/api/server`)

- [ ] **Step 3: Implement the routes, the `server` step-up purpose, the control wiring and the health field**

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check . ../scripts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server
git commit -m "feat(api): stop and restart Hearthkeep from the app, with a passkey step-up"
```

---

### Task 5: The web bundle: release checkouts, `main` builds, and the release job

**Files:**
- Create: `server/src/hearthkeep/webbundle.py`, `scripts/build_release.py`, `.github/workflows/release.yml`
- Modify: `server/src/hearthkeep/api/app.py` (`DEFAULT_WEB_DIST = webbundle.WEB_DIST`, no second path rule), `.gitignore` (`.cache/`, `/hearthkeep-*.zip`, `/hearthkeep-web-*.zip`, `*.zip.sha256`, `web/dist.new/`, `web/dist.old/`), `CONTRIBUTING.md` (new "Releasing" section: the version lives in `server/pyproject.toml` and `hearthkeep/__init__.py`; tag `vX.Y.Z` on that commit; the workflow makes a draft release with four assets; write the notes, publish; then set `main` to `X.Y.(Z+1).dev0`)
- Test: `server/tests/test_webbundle.py`, `server/tests/test_build_release.py`

**Interfaces:**
- Consumes: Task 1 `REPO_ROOT`; `procutil.find_executable`.
- Produces (`webbundle.py`):
  - `WEB_DIR = REPO_ROOT / "web"`; `WEB_DIST = WEB_DIR / "dist"`; `MANIFEST = ".bundle.json"` (`{"version": v, "files": {"<posix path>": "<sha256>", …}}`, sorted, the manifest itself excluded); `MAX_BUNDLE_BYTES = 50_000_000`; `MAX_BUNDLE_FILES = 2000`.
  - `release_version(version: str = hearthkeep.__version__) -> str | None`: the version when it matches `^\d+\.\d+\.\d+$` and isn't `0.0.0`, else None.
  - `bundle_name(v) -> str` = `f"hearthkeep-web-{v}.zip"`; `release_zip_name(v)` = `f"hearthkeep-{v}.zip"`; `checksum_line(sha: str, name: str) -> str` = `f"{sha}  {name}\n"`; `parse_checksum(text: str, name: str) -> str | None` (64 lowercase hex for that name, else None).
  - `build_manifest(dist: Path, version: str) -> dict`; `write_manifest(dist, version) -> None`; `verify_dist(dist: Path, version: str) -> bool` (manifest version equal, same file set, every hash equal).
  - `github_repo(*, run=subprocess.run, root: Path = REPO_ROOT) -> tuple[str, str] | None`: `git -C <root> remote get-url origin` (git from `find_executable("git")`, `NO_WINDOW`, timeout 10); accepts `https://github.com/<o>/<r>(.git)` and `git@github.com:<o>/<r>(.git)` with `<o>`/`<r>` in `[A-Za-z0-9_.-]+`; anything else None.
  - `safe_extract(zip_path: Path, dest: Path) -> None`: refuses (`BundleError`) absolute names, drive letters, `..` parts, backslashes, symlink entries, more than `MAX_BUNDLE_FILES` entries or more than `MAX_BUNDLE_BYTES` uncompressed.
  - `download(url: str, dest: Path, *, client: httpx.Client) -> None`: `https://` only, redirects followed one at a time and each must stay `https://`, streamed, stopped past `MAX_BUNDLE_BYTES`.
  - `BundleOutcome = Literal["ready", "downloaded", "built", "existing", "failed"]`; `class BundleError(Exception)`.
  - `ensure_web_bundle(*, say: Callable[[str], None], version: str = hearthkeep.__version__, dist: Path = WEB_DIST, run=subprocess.run, client: httpx.Client | None = None, find=find_executable) -> BundleOutcome`:
    - **Release version `v`.** `verify_dist(dist, v)` → `"ready"`. Else `github_repo()`; None → say `NO_BUNDLE_NO_REPO.format(v=v)`, `"failed"`. Else download `https://github.com/<o>/<r>/releases/download/v<v>/<bundle_name>` and `…/<bundle_name>.sha256` into `REPO_ROOT/.cache/`; a network error → say `BUNDLE_DOWNLOAD_FAILED`, `"failed"`; a wrong or missing checksum → delete, say `BUNDLE_BAD_CHECKSUM`, `"failed"`. Then `safe_extract` into `dist.new`, `write_manifest`, swap (`dist` → `dist.old`, `dist.new` → `dist`, remove `dist.old`), say `BUNDLE_READY`, `"downloaded"`.
    - **Development version.** `npm = find("npm.cmd") or find("npm")`. With npm: say `BUILDING`; run `[npm, "ci"]` then `[npm, "run", "build"]` with `cwd=WEB_DIR` (output to the console); a non-zero exit → say `BUILD_FAILED`, `"failed"`; else `"built"`. Without npm: `dist/index.html` exists → say `USING_EXISTING_BUILD`, `"existing"`; else say `MAIN_IS_FOR_DEVELOPERS` and then the latest-release link (`https://github.com/<o>/<r>/releases/latest`) or, without a repo, `"the Releases page of the Hearthkeep project on GitHub"`; `"failed"`.
  - Copy (exact, ASCII):
    - `BUNDLE_READY = "Hearthkeep's web app is ready."`
    - `BUNDLE_DOWNLOAD_FAILED = "Hearthkeep couldn't download its web app. Check your internet connection, then double-click Setup.cmd again."`
    - `BUNDLE_BAD_CHECKSUM = "The downloaded web app didn't match the release's checksum, so it wasn't used. Double-click Setup.cmd to try again."`
    - `NO_BUNDLE_NO_REPO = "This copy of Hearthkeep is missing its web app. Download hearthkeep-{v}.zip from the Releases page of the Hearthkeep project on GitHub, unzip it, and double-click Setup.cmd in that folder."`
    - `BUILDING = "Building the web app with Node.js (developers' version). This takes a minute or two..."`
    - `BUILD_FAILED = "The web app didn't build. The messages above say why."`
    - `USING_EXISTING_BUILD = "Node.js isn't installed, so the web app that was already built is used as it is."`
    - `MAIN_IS_FOR_DEVELOPERS = "This copy of Hearthkeep is the developers' version, which needs Node.js to build its web app. To install Hearthkeep, download the latest release instead:"`
- Produces (`scripts/build_release.py`, run as `uv run --project server python ../scripts/build_release.py --version V --out DIR` from `server/`):
  - `main(argv=None, *, git_archive=…) -> int`. Refuses (exit 2) unless `release_version(V) == V == hearthkeep.__version__` and `web/dist/index.html` exists.
  - Writes the manifest into `web/dist`, then `DIR/hearthkeep-web-V.zip` (the files of `web/dist` at the zip root, sorted, timestamps `1980-01-01 00:00:00`, mode `0o644`) and `DIR/hearthkeep-V.zip` (`git archive --format=zip --prefix=hearthkeep-V/ HEAD`, then `web/dist` appended under `hearthkeep-V/web/dist/` the same way), and a `.sha256` beside each in `checksum_line` format.
- Produces (`release.yml`): `on: push: tags: ["v*.*.*"]`, `permissions: contents: write`, one `ubuntu-latest` job with the same pinned `actions/checkout`, `actions/setup-node` (node 22) and `astral-sh/setup-uv` SHAs as `ci.yml`:
  1. `npm ci`, `npm test` and `npm run build` in `web`;
  2. `uv run --project server python scripts/build_release.py --version "${GITHUB_REF_NAME#v}" --out "$RUNNER_TEMP/release"` (it fails when the tag and `__version__` differ);
  3. `gh release create "$GITHUB_REF_NAME" "$RUNNER_TEMP"/release/* --draft --verify-tag --title "Hearthkeep $GITHUB_REF_NAME" --notes "Release notes are on the way."` with `GH_TOKEN: ${{ github.token }}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_version_matches_pyproject():
    pyproject = tomllib.loads((REPO_ROOT / "server" / "pyproject.toml").read_text())
    assert hearthkeep.__version__ == pyproject["project"]["version"]
@pytest.mark.parametrize("v, out", [("0.1.0", "0.1.0"), ("0.0.0", None), ("0.2.0.dev0", None), ("1.0", None)])
def test_release_version(v, out)
def test_release_checkout_with_a_verified_bundle_is_ready(tmp_path):     # write_manifest then ensure → "ready", no network (client that raises)
def test_tampered_bundle_file_is_not_ready(tmp_path):                    # change one byte → verify_dist False → downloads
def test_tag_clone_downloads_and_verifies(tmp_path):
    zip_bytes = make_zip({"index.html": b"<html>", "assets/a.js": b"x"})
    client = mock_client({".../hearthkeep-web-0.1.0.zip": zip_bytes,
                          ".../hearthkeep-web-0.1.0.zip.sha256": checksum_line(sha(zip_bytes), "hearthkeep-web-0.1.0.zip")})
    assert ensure(tmp_path, "0.1.0", client, origin="https://github.com/example/hearthkeep.git") == "downloaded"
    assert verify_dist(tmp_path / "dist", "0.1.0") and client.urls[0].startswith("https://github.com/example/hearthkeep/releases/download/v0.1.0/")
def test_bad_checksum_is_refused_and_dist_untouched(tmp_path)           # "failed", BUNDLE_BAD_CHECKSUM said, old dist still there
def test_http_redirect_is_refused(tmp_path)                               # a 302 to http:// → "failed"
@pytest.mark.parametrize("name", ["../x", "/abs", "C:/x", "a\\b"])
def test_safe_extract_refuses(tmp_path, name)                             # and a 60 MB entry, and 2001 entries
def test_main_with_node_builds(tmp_path)                                  # fake find → npm; run gets [npm,"ci"] then [npm,"run","build"], cwd WEB_DIR
def test_main_without_node_uses_existing_build_or_explains(tmp_path):
    said = []
    assert ensure_dev(tmp_path, find=lambda *a: None, say=said.append, origin=None) == "failed"
    assert said[0] == MAIN_IS_FOR_DEVELOPERS and "Releases page" in said[1]
    (tmp_path / "dist").mkdir(); (tmp_path / "dist" / "index.html").write_text("")
    assert ensure_dev(tmp_path, find=lambda *a: None, say=said.append, origin=None) == "existing"
def test_github_repo_parses_origin()                                      # https and ssh forms; gitlab, odd owners → None
def test_bundle_copy_is_ascii_and_has_no_commands()

# test_build_release.py
def test_build_release_makes_both_zips_and_checksums(tmp_path, monkeypatch):
    # fake web/dist + fake git_archive writing a zip with "hearthkeep-0.1.0/README.md"; __version__ patched to 0.1.0
    assert build_release.main(["--version", "0.1.0", "--out", str(out)], git_archive=fake) == 0
    for name in ("hearthkeep-web-0.1.0.zip", "hearthkeep-0.1.0.zip"):
        assert parse_checksum((out / f"{name}.sha256").read_text(), name) == sha256_of(out / name)
    assert "hearthkeep-0.1.0/web/dist/.bundle.json" in zipfile.ZipFile(out / "hearthkeep-0.1.0.zip").namelist()
    safe_extract(out / "hearthkeep-web-0.1.0.zip", tmp_path / "x"); assert verify_dist(tmp_path / "x", "0.1.0")
def test_build_release_refuses_a_mismatched_version(tmp_path)              # --version 0.2.0 with __version__ 0.1.0 → 2
def test_web_zip_is_reproducible(tmp_path)                                 # two builds → identical bytes
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_webbundle.py tests/test_build_release.py -q`
Expected: FAIL (`ModuleNotFoundError: hearthkeep.webbundle`)

- [ ] **Step 3: Implement `webbundle.py`, the script and the workflow; point `app.DEFAULT_WEB_DIST` at `WEB_DIST`; add the `.gitignore` lines and the CONTRIBUTING section**

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check . ../scripts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server scripts .github .gitignore CONTRIBUTING.md
git commit -m "feat(release): ship a verified prebuilt web bundle, and build it on main with Node"
```

---

### Task 6: Setup mode and `hearthkeep setup`

**Files:**
- Create: `server/src/hearthkeep/setupmode.py`, `server/src/hearthkeep/api/routes_setup.py`
- Modify:
  - `server/src/hearthkeep/api/app.py` (`create_app(..., setup: SetupMode | None = None)`; `_install_static` serves `/setup`; include the router before the static routes);
  - `server/src/hearthkeep/api/common.py` (`Services.setup`);
  - `server/src/hearthkeep/api/routes_auth.py` (`enroll_verify` completes setup);
  - `server/src/hearthkeep/cli.py` (`cmd_setup`, the `setup` parser entry, `port_free`; `build_app` passes `setup`);
  - `docs/security.md:22` (the first passkey is normally made through setup mode: a one-time secret in a URL fragment, only from this PC, only before any passkey exists);
  - `server/tests/test_api.py` (the harness gains `setup=None` and `peer=("127.0.0.1", 50000)`, passed as `TestClient(app, base_url=ORIGIN, client=peer)`).
- Test: `server/tests/test_setup_mode.py`

**Interfaces:**
- Consumes: Task 1 `current_host`, `Host.check_data_dir`, `Host.default_data_dir`, `Host.write_location`, `Host.install_shortcut`, `Host.spawn_detached`; Task 3 `ServerControl`, `run_server`, `build_app`, `launcher_argv`, `wait_for_health`, `open_app`, `EXIT_STOPPED`; Task 4 `Services.control`; Task 5 `ensure_web_bundle`.
- Produces (`setupmode.py`):
  - `class SetupMode`: `__init__(self, secret: str | None = None)` (default `secrets.token_urlsafe(32)`); `active: bool` (secret held and not completed); `completed: bool`; `secret: str | None`, used only to build the link; `check(candidate: str) -> bool` (`hmac.compare_digest` on UTF-8 bytes; False when not active); `complete() -> None` (drops the secret, sets `completed`).
  - `setup_url(origin: str, secret: str) -> str` = `f"{origin.rstrip('/')}/setup#{secret}"`.
- Produces (`routes_setup.py`):
  - `class SetupBody(BaseModel): secret: str = Field(min_length=1, max_length=128)`.
  - `_gate(request, body) -> Services`, applied in this order:
    1. `hk.setup` None or not active, or `first_device_allowed(hk.conn)` false → 404 `not_found`.
    2. `client_ip(request)` not in `{"127.0.0.1", "::1"}` → 403 `loopback_only`.
    3. `rate_limit(request, "setup")`.
    4. `hk.setup.check(body.secret)` false → audit `setup.denied` `{"reason": "bad_secret", "client": ip}`, 403 `setup_secret`.
  - `POST /api/setup/status` → `{"state": "needs_passkey"}`.
  - `POST /api/setup/enrollment-code` → `hk.codes.issue("first_device", ENROLLMENT_CODE_TTL_MINUTES)`; `JSONResponse({"code", "expires_in_minutes"}, headers={"Cache-Control": "no-store"})`. Its audit is today's `enrollment.code_issued` `{"purpose"}`, never the code.
- Produces (`app.py`): `GET /setup` → `index.html` with `Cache-Control: no-store` while setup is active and no credential exists, else `RedirectResponse("/", 302)`. With setup given, the app's control is `ServerControl(supervised=False, setup_mode=True)` unless one is passed.
- Produces (`routes_auth.enroll_verify`): after a successful `first_device` enrolment with `hk.setup` active → `hk.setup.complete()`, and the 201 response gets `BackgroundTask(hk.control.request, EXIT_STOPPED)`.
- Produces (`cli.py`):
  - `port_free(host: str, port: int) -> bool`: a bind test on a socket closed at once.
  - `cmd_setup(args, *, host=None, open_url=webbrowser.open, ensure_bundle=ensure_web_bundle, make_verifier=PyWebAuthnVerifier, wait=wait_for_health, opener=open_app, make_llama=…, make_comfy=…, detect_vram=…) -> int`, in order:
    1. Print `SETUP_INTRO`. An unsupported host → print `UNSUPPORTED_HOST`, return `EXIT_USAGE` (nothing created).
    2. `data = Path(args.data) if args.data else host.default_data_dir()`; `problem = host.check_data_dir(data)` → print it, return 1.
    3. Print `STEP_WEB`; `ensure_bundle(say=print)` == `"failed"` → return 1.
    4. `init_data_dir(data)`; `host.write_location(data)`; `settings = _settings(data)`. (Task 8 adds `run_upgrade` here.)
    5. A credential exists → print `ALREADY_SET_UP`; `return opener(settings, host, open_url=open_url)`.
    6. `not port_free(...)` → print `PORT_BUSY.format(port=settings.port)`, return 1.
    7. Print `STEP_START`. `setup = SetupMode()`; `control = ServerControl(supervised=False, setup_mode=True)`; `app, lines = build_app(settings, verifier=make_verifier(settings), control=control, setup=setup, …)`; print `lines`; `run_server(app, host_ip, settings.port, control=control, on_started=announce)`. `announce` prints `BROWSER_OPENING` and `KEEP_OPEN` when `open_url(setup_url(settings.public_origin, secret))` returns True; when it returns False or raises, it prints `BROWSER_FAILED` then the link on its own line.
    8. `setup.completed` false → print `STOPPED_EARLY`, return 1.
    9. `host.install_shortcut(data)` (a `ShortcutError` prints `SHORTCUT_FAILED` and goes on). `host.spawn_detached(launcher_argv(data))`. If `wait(f"http://127.0.0.1:{port}", want_setup_mode=False)` → print `FINISHED`, else `FINISHED_SLOW`. Return 0.
  - Parser: `setup` with an optional `--data` (help "set up Hearthkeep on this PC (used by Setup.cmd)").
  - Copy (exact, ASCII):
    - `SETUP_INTRO = "Hearthkeep setup\n================\nThis window sets up Hearthkeep on this PC."`
    - `STEP_WEB = "Step 2 of 3: Getting Hearthkeep's web app ready..."`
    - `STEP_START = "Step 3 of 3: Starting Hearthkeep..."`
    - `BROWSER_OPENING = "Your browser is opening Hearthkeep's setup page."`
    - `KEEP_OPEN = "Keep this window open until setup is finished. Closing it stops setup."`
    - `BROWSER_FAILED = "Your browser didn't open. Open this link in your browser to continue setup. It works only while this window is open:"`
    - `ALREADY_SET_UP = "Hearthkeep is already set up on this PC. Opening it in your browser..."`
    - `PORT_BUSY = "Another program on this PC is using Hearthkeep's address (port {port}). Close it, or restart the PC, then double-click Setup.cmd again."`
    - `STOPPED_EARLY = "Setup stopped before it was finished. Double-click Setup.cmd to start again."`
    - `SHORTCUT_FAILED = "The Start menu shortcut couldn't be made. Hearthkeep tries again the next time it starts."`
    - `FINISHED = "Setup is finished. Hearthkeep keeps running in the background; open it from the Start menu any time. You can close this window now."`
    - `FINISHED_SLOW = "Setup is finished, but Hearthkeep is taking a while to start. Open it from the Start menu in a minute. You can close this window now."`

- [ ] **Step 1: Write the failing tests**

```python
def setup_client(harness, secret="s3cret-" + "x" * 30, **kw):
    return harness.start(setup=SetupMode(secret), **kw), secret

def test_setup_code_needs_the_secret_and_loopback(harness):
    client, secret = setup_client(harness)
    before = harness.query("SELECT COUNT(*) FROM enrollment_codes")
    for body, params in (({}, None), ({"secret": "wrong"}, None), ({"secret": ""}, None), ({}, {"secret": secret})):
        r = client.post("/api/setup/enrollment-code", json=body, params=params, headers=CSRF)
        assert r.status_code in (403, 422)
    assert client.post("/api/setup/enrollment-code", json={"secret": secret}).status_code == 403   # no CSRF header → csrf
    remote = harness.start(setup=SetupMode(secret), peer=("192.168.1.20", 50000))
    assert remote.post("/api/setup/enrollment-code", json={"secret": secret}, headers=CSRF).json()["reason"] == "loopback_only"
    assert harness.query("SELECT COUNT(*) FROM enrollment_codes") == before
    ok = client.post("/api/setup/enrollment-code", json={"secret": secret}, headers=CSRF)
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store" and len(ok.json()["code"]) == 8
    for _ in range(12):
        last = client.post("/api/setup/status", json={"secret": "guess"}, headers=CSRF)
    assert last.status_code == 429                                  # the guessing loop is cut off

def test_setup_rerun_after_passkey_opens_app_and_setup_is_gone(harness, tmp_path, monkeypatch):
    control = ServerControl(supervised=False, setup_mode=True); control.attach(lambda: None)
    client, secret = setup_client(harness, control=control, web_dist=dist_with_index(tmp_path))
    assert client.get("/setup", follow_redirects=False).status_code == 200
    code = client.post("/api/setup/enrollment-code", json={"secret": secret}, headers=CSRF).json()["code"]
    enroll(client, code)                                          # the existing passkey flow, FakeVerifier
    assert control.exit_code == EXIT_STOPPED                      # the setup server hands over
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/"
    for path in ("/api/setup/status", "/api/setup/enrollment-code"):
        assert client.post(path, json={"secret": secret}, headers=CSRF).status_code == 404
    # cmd_setup on that data folder: no setup-mode server, opener called once
    calls = []
    assert run_cmd_setup(harness.data, opener=lambda s, h, **k: calls.append(s.public_origin) or 0) == 0
    assert calls == ["http://localhost:8765"] and fake_run_server.calls == []

def test_setup_secret_and_code_never_logged(tmp_path, caplog, capsys):
    # cmd_setup end to end with fakes: ensure_bundle → "ready"; open_url returns False (so the link is printed once);
    # fake run_server drives the app with TestClient(peer loopback): status, code, enroll, then returns control.exit_code
    caplog.set_level(logging.DEBUG)
    assert run_cmd_setup(tmp_path / "data", open_url=lambda url: False) == 0
    out = capsys.readouterr().out
    secret, code = fake_run_server.secret, fake_run_server.code
    assert out.count(secret) == 1 and f"/setup#{secret}" in out and "?" not in out.split(secret)[0][-40:]
    assert code not in out and secret not in caplog.text and code not in caplog.text
    assert not any(secret in d or code in d for (d,) in query(tmp_path / "data", "SELECT detail FROM audit"))
    assert FINISHED in out and spawned[0][3:5] == ["launch", "--data"]

def test_setup_stopped_before_passkey_says_so(tmp_path, capsys)   # fake run_server returns without enrolling → 1, STOPPED_EARLY, nothing spawned
def test_setup_refuses_a_bad_data_folder_before_creating_it(tmp_path)  # check_data_dir message → 1, folder not created, no bundle step
def test_setup_stops_when_the_bundle_fails(tmp_path)                # ensure_bundle "failed" → 1, no server
def test_setup_port_busy(tmp_path, capsys)                          # a socket bound on the port → PORT_BUSY, 1
def test_setup_on_other_os_creates_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(startup, "is_windows", lambda: False)
    assert cli.main(["setup", "--data", str(tmp_path / "d")]) == EXIT_USAGE
    assert UNSUPPORTED_HOST in capsys.readouterr().out and not (tmp_path / "d").exists()
def test_setup_copy_is_ascii_without_commands()                     # every constant above: isascii, no "hearthkeep ", "uv run", "--data", "<"
def test_health_says_setup_mode_while_setting_up(harness)           # /api/health setup_mode true, false after complete via a normal app
```

`run_cmd_setup(data, **over)` calls `cli.cmd_setup` with a `WindowsHost` subclass whose `check_data_dir` skips the drive-letter rule (Linux paths), whose `install_shortcut` and `spawn_detached` record, and with `make_verifier=lambda s: FakeVerifier()`, `wait=lambda *a, **k: True`, and `cli.run_server` monkeypatched to `fake_run_server`.

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_setup_mode.py -q`
Expected: FAIL (`ModuleNotFoundError: hearthkeep.setupmode`)

- [ ] **Step 3: Implement `setupmode.py`, the setup routes, the `/setup` route, the hand-over in `enroll_verify`, `cmd_setup` and the security.md line; add `peer` and `setup` to the harness**

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check . ../scripts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server docs
git commit -m "feat(setup): a secret-protected setup mode hands the first passkey code to the browser"
```

---

### Task 7: `Setup.cmd`

**Files:**
- Create: `Setup.cmd` (CRLF line endings, ASCII), `.gitattributes` (`*.cmd text eol=crlf`; keep any existing lines)
- Modify: `README.md` (the quick start becomes: download `hearthkeep-<version>.zip` from the latest release, unzip it, double-click `Setup.cmd`; the developer commands move under "Developers" with a link to CONTRIBUTING), `docs/install.md` (§2 opens with the same three steps and says the rest of the guide is the manual way; §8 says the first passkey is normally made by the setup page)
- Test: `server/tests/test_setup_cmd.py`

**Interfaces:**
- Consumes: Task 6 `hearthkeep setup` (its exit code).
- Produces (`Setup.cmd`): the script is shell glue with exact values, so its body is fixed here. `UV_VERSION` is the uv version on the reference host at implementation time. `UV_INSTALLER_SHA256` is the uppercase output of `powershell -NoProfile -Command "(Invoke-WebRequest -UseBasicParsing https://astral.sh/uv/<UV_VERSION>/install.ps1).Content | Set-Content -NoNewline -Encoding Byte x.ps1; (Get-FileHash x.ps1).Hash"` (or `Get-FileHash` on a downloaded copy).

```bat
@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Hearthkeep setup
cd /d "%~dp0"
set "UV_VERSION=X.Y.Z"
set "UV_INSTALLER_SHA256=<64 uppercase hex>"
set "UV_INSTALL_DIR=%USERPROFILE%\.local\bin"
set "UV_EXE=%UV_INSTALL_DIR%\uv.exe"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
echo.
echo Step 1 of 3: Checking for uv, the tool that runs Hearthkeep...
if exist "%UV_EXE%" goto run
for /f "delims=" %%U in ('where uv 2^>nul') do (set "UV_EXE=%%U" & goto run)
echo Installing uv for your Windows account. No admin rights are needed.
"%PS%" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $f=Join-Path $env:TEMP ('uv-install-'+[guid]::NewGuid()+'.ps1'); Invoke-WebRequest -UseBasicParsing -Uri ('https://astral.sh/uv/'+$env:UV_VERSION+'/install.ps1') -OutFile $f; if ((Get-FileHash -Algorithm SHA256 $f).Hash -ne $env:UV_INSTALLER_SHA256) { Remove-Item $f; exit 3 }; $env:UV_NO_MODIFY_PATH='1'; & $f; $c=$LASTEXITCODE; Remove-Item $f; exit $c"
if errorlevel 3 if not errorlevel 4 (echo The uv installer didn't match its expected checksum, so it was not run. Try again later. & goto end)
if not exist "%UV_EXE%" (echo uv couldn't be installed. Check your internet connection, then double-click Setup.cmd again. & goto end)
:run
"%UV_EXE%" run --project server hearthkeep setup
:end
echo.
echo Press any key to close this window.
pause >nul
```

- [ ] **Step 1: Write the failing test**

```python
CMD = REPO_ROOT / "Setup.cmd"
def test_setup_cmd_is_crlf_ascii():
    raw = CMD.read_bytes()
    assert raw.isascii() and b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
def test_setup_cmd_pins_and_checks_the_uv_installer():
    text = CMD.read_text()
    assert re.search(r'^set "UV_VERSION=\d+\.\d+\.\d+"$', text, re.M)
    assert re.search(r'^set "UV_INSTALLER_SHA256=[0-9A-F]{64}"$', text, re.M)
    assert "https://astral.sh/uv/'+$env:UV_VERSION+'/install.ps1" in text and "Get-FileHash -Algorithm SHA256" in text
    assert "UV_NO_MODIFY_PATH" in text and r"System32\WindowsPowerShell\v1.0\powershell.exe" in text
    for banned in ("iex", "Invoke-Expression", "Set-ExecutionPolicy", "runas", "-Verb"):
        assert banned not in text
def test_setup_cmd_runs_setup_and_keeps_the_window_open():
    text = CMD.read_text()
    assert '"%UV_EXE%" run --project server hearthkeep setup' in text
    assert text.rstrip().endswith("pause >nul") and "Press any key to close this window." in text
def test_gitattributes_keeps_cmd_crlf():
    assert "*.cmd text eol=crlf" in (REPO_ROOT / ".gitattributes").read_text()
```

- [ ] **Step 2: Run it to see it fail**

Run: `cd server && uv run pytest tests/test_setup_cmd.py -q`
Expected: FAIL (`Setup.cmd` missing)

- [ ] **Step 3: Write `Setup.cmd` (CRLF), `.gitattributes`, the README quick start and the install.md lines**

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check . ../scripts`
Expected: PASS

- [ ] **Step 5: Check by hand on the reference host**

On a Windows account without uv: copy the repo to a short path, build `web/dist` once, and double-click `Setup.cmd`. Check that:
- uv installs;
- the browser opens `/setup` and the fragment disappears from the address bar;
- "Create passkey" works with Windows Hello;
- the console says setup is finished and the app opens at `/`;
- after closing the console, Hearthkeep still answers;
- the Start-menu "Hearthkeep" entry opens it;
- a second double-click says "already set up".

Use a throwaway data folder by passing `--data` to the `uv run` line by hand: this is a developer step, not owner copy.

Expected: every console line is plain English with no command, and the setup link appears only if the browser didn't open.

- [ ] **Step 6: Commit**

```bash
git add Setup.cmd .gitattributes README.md docs server/tests/test_setup_cmd.py
git commit -m "feat(setup): double-click Setup.cmd to install uv and start setup"
```

---

### Task 8: The upgrade migration and the "Hearthkeep was updated" note

**Files:**
- Create: `server/src/hearthkeep/upgrade.py`, `server/tests/fixtures/upgrade/old-task.xml` (today's `task_xml` output with `{pythonw}` and `{data}` placeholders the test fills; arguments `-m hearthkeep serve --data "{data}" --log-file "{data}\logs\hearthkeep.log"`)
- Modify:
  - `server/src/hearthkeep/db.py` (migration 10: `CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT);`);
  - `server/src/hearthkeep/cli.py` (`cmd_serve` and `cmd_setup` call `run_upgrade` after `_settings`; a failure is logged by type and never stops the start);
  - `server/src/hearthkeep/api/routes_server.py` (the note routes);
  - `docs/install.md` §14 Updating (what the first start after an update does, and the note).
- Test: `server/tests/test_upgrade.py`

**Interfaces:**
- Consumes: Task 1 `Host`; Task 2 `StartupStatus.launcher`, `Host.install_shortcut`; project 1 `Addons.get`, `permissions.DEFAULT_LEVELS`.
- Produces (`upgrade.py`):
  - `UPGRADE_LEVEL = 1`; `PLACEHOLDER_LEVELS = ("wallet", "news", "demo")`; app_state keys `"upgrade_level"` and `"update_note"`.
  - `@dataclass(frozen=True) UpgradeReport(ran: bool, changed: tuple[str, ...], items: tuple[str, ...])`.
  - `run_upgrade(data_dir: Path, conn: sqlite3.Connection, *, host: Host, run=subprocess.run, version: str = hearthkeep.__version__) -> UpgradeReport`. A stored `upgrade_level >= UPGRADE_LEVEL` returns `UpgradeReport(False, (), ())` without doing anything. `existing` = any row in `credentials` or `tool_levels` before this run. Steps, each idempotent and each in its own `try` (a failure logs the exception type and adds its item):
    1. **location** (host supported): `host.read_location()` None or not an existing folder → `host.write_location(data_dir)` (changed `"location"`).
    2. **startup** (host supported): `s = host.startup_status(run=run)`; when `s.enabled and s.serves(data_dir.resolve()) and not s.launcher` → `host.enable_startup(data_dir, run=run)` (changed `"startup"`). On another folder or off: left alone. `StartupError` → item `"startup_not_moved"`.
    3. **shortcut** (host supported and `existing`): `host.install_shortcut(data_dir, run=run)` (changed `"shortcut"`); `ShortcutError` → item `"shortcut_failed"`.
    4. **levels**: delete `tool_levels` rows whose `tool` is in `PLACEHOLDER_LEVELS` and not in `DEFAULT_LEVELS`, in one transaction with audit `upgrade.levels_removed` `{"tools": sorted(removed)}` when any (changed `"levels"`).
    
    Then, when no step failed, `upgrade_level = UPGRADE_LEVEL`. When `existing`, `update_note` = `{"version": version, "items": sorted(set(stored items) | set(items))}`, written whether or not a step failed. Audit `upgrade.ran` `{"level": 1, "changed": [...], "items": [...]}`. Config files, cards, credentials, sessions and other level rows are never touched.
  - `NOTE_ITEMS: dict[str, tuple[str, str | None]]` (id → text, add-on id for the button):
    - `"sandbox_image"`: `("The analysis sandbox needs rebuilding.", "analysis")`
    - `"startup_not_moved"`: `("Start at login couldn't be updated. Hearthkeep tries again the next time it starts.", "startup")`
    - `"shortcut_failed"`: `("The Start menu shortcut couldn't be made. Hearthkeep tries again the next time it starts.", None)`
  - `update_note(conn, addons: Addons | None) -> dict | None`: None without a stored note. Otherwise `{"version", "items": [{"id", "text", "addon"}]}`: the stored items still in `NOTE_ITEMS`, plus `sandbox_image` when `addons.get("analysis").state == "needs_image"`.
  - `dismiss_update_note(conn) -> bool` (deletes the row).
- Produces (`routes_server.py`): `GET /api/update-note` (session) → `{"note": update_note(...)}`; `POST /api/update-note/dismiss` (session) → `{"ok": true}`, audit `update_note.dismissed` `{}` when one was there.

- [ ] **Step 1: Write the failing tests**

```python
def todays_install(tmp_path):
    """A data folder as Phase 4 leaves it: fixtures config.toml/models.toml/card, a schema-9 database
    with a credential, a session and levels python=ask, wallet=off, news=allowed, demo=ask."""
    ...
@pytest.mark.real_levels
def test_upgrade_moves_task_drops_placeholders_and_keeps_the_rest(tmp_path):
    data = todays_install(tmp_path)
    before = snapshot(data)                        # bytes of config.toml, models.toml, cards; credentials, sessions rows
    host = FakeWindowsHost(task=old_task_xml(data))  # startup_status parses the fixture; enable/install record
    report = run_upgrade(data, connect(data / DB_FILE), host=host)
    assert report.changed == ("location", "startup", "shortcut", "levels") and report.items == ()
    assert host.enabled == [data] and host.shortcuts == [data] and host.location == data
    assert levels(data) == {"python": "ask"} and snapshot(data) == before
    assert update_note(conn, addons_with(analysis="ready")) == {"version": hearthkeep.__version__, "items": []}

@pytest.mark.real_levels
def test_upgrade_twice_changes_nothing_the_second_time(tmp_path):
    data = todays_install(tmp_path); host = FakeWindowsHost(task=old_task_xml(data))
    run_upgrade(data, conn := connect(data / DB_FILE), host=host)
    audit_rows, note = count(conn, "audit"), conn.execute("SELECT value FROM app_state WHERE key='update_note'").fetchone()
    second = run_upgrade(data, conn, host=host)
    assert second.ran is False and len(host.enabled) == 1 and len(host.shortcuts) == 1
    assert count(conn, "audit") == audit_rows and conn.execute("SELECT value FROM app_state WHERE key='update_note'").fetchone() == note

def test_failed_step_retries_next_start(tmp_path):
    host = FakeWindowsHost(task=old_task_xml(data), enable_fails=True)
    first = run_upgrade(data, conn, host=host)
    assert first.items == ("startup_not_moved",)
    host.enable_fails = False
    second = run_upgrade(data, conn, host=host)
    assert second.ran and "startup" in second.changed
    assert [i["id"] for i in update_note(conn, None)["items"]] == ["startup_not_moved"]   # once, not twice
    assert run_upgrade(data, conn, host=host).ran is False

def test_task_for_another_folder_or_already_launcher_is_left_alone(tmp_path)
def test_fresh_folder_gets_no_note_and_no_shortcut(tmp_path)        # init_data_dir only → ran, no "shortcut", update_note None
def test_demo_row_kept_while_demo_is_a_default_level(tmp_path)      # without real_levels: conftest's demo default → demo row stays
def test_other_os_migration_only_touches_the_database(tmp_path, monkeypatch)  # UnsupportedHost: no location/startup/shortcut calls; levels still dropped
def test_sandbox_item_appears_while_analysis_needs_image(tmp_path)
def test_update_note_routes(harness):                                # 401 without session; GET shows note; dismiss → {"note": null}; audit update_note.dismissed
def test_serve_runs_the_upgrade_and_still_starts_when_it_fails(serve, monkeypatch)   # run_upgrade raising → serve() == 0, warning logged by type
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && uv run pytest tests/test_upgrade.py -q`
Expected: FAIL (`ModuleNotFoundError: hearthkeep.upgrade`)

- [ ] **Step 3: Implement migration 10, `upgrade.py`, the calls from `serve` and `setup`, the note routes, the fixture and the install.md §14 lines**

- [ ] **Step 4: Run the tests and the whole server suite**

Run: `cd server && uv run pytest -q && uv run ruff check . ../scripts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server docs
git commit -m "feat(upgrade): a one-time migration for existing installs, and a note on what needs the owner"
```

---

### Task 9: Web: the `/setup` page, stop and restart, and the update note

**Files:**
- Create: `web/src/api/setup.ts`, `web/src/api/server.ts`, `web/src/views/SetupPage.tsx`, `web/src/components/ServerPanel.tsx`, `web/src/components/RestartNotice.tsx`, `web/src/components/UpdateNote.tsx`
- Modify:
  - `web/src/App.tsx` (the `/setup` route);
  - `web/src/api/webauthn.ts` (`StepUpPurpose` gains `'server'`);
  - `web/src/shell/AddonsContext.tsx` (`SettingsAnchor` gains `'settings-general'`);
  - `web/src/views/Settings.tsx` (`<ServerPanel/>` after the Start at login panel);
  - `web/src/components/AddonsPanel.tsx` (`<RestartNotice/>` at the top);
  - `web/src/shell/AppShell.tsx` (`<UpdateNote/>` above the current view);
  - `web/src/test/fetch.ts` (`serverInfo(over?)`, `noUpdateNote`).
- Test: `web/src/__tests__/setup.test.tsx`, `web/src/__tests__/server.test.tsx`, existing `settings.test.tsx`, `shell.test.tsx`, `signin.test.tsx`

**Interfaces:**
- Consumes: Task 4 `/api/server*`; Task 6 `/api/setup/*`, `/api/health` `setup_mode`; Task 8 `/api/update-note*`; `enroll`, `stepUp`, `passkeyErrorMessage` (`api/webauthn.ts`); `guessDeviceName` (`views/SignIn.tsx`); `useOpenSettings` (`shell/AddonsContext.tsx`).
- Produces (`api/setup.ts`): `readSetupSecret(): string | null` reads `location.hash`, keeps it only if it matches `/^#[A-Za-z0-9_-]{20,128}$/`, and always calls `history.replaceState(null, '', '/setup')` when a hash was there. Also `setupStatus(secret: string): Promise<{ state: 'needs_passkey' }>` and `setupEnrollmentCode(secret: string): Promise<{ code: string; expires_in_minutes: number }>`, both POSTs with `{ secret }` in the body.
- Produces (`api/server.ts`):
  - `type ServerInfo = { version: string; supervised: boolean; setup_mode: boolean; started_at: string; restart_needed: boolean; restart_reasons: string[] }`;
  - `getServer()`, `restartServer(token)`, `stopServer(token)`;
  - `waitForRestart(previous: string, { timeoutMs = 60000, intervalMs = 1000 } = {}): Promise<boolean>`: true once `getServer()` answers with another `started_at`;
  - `waitForSetupDone({ timeoutMs = 60000, intervalMs = 1000 } = {}): Promise<boolean>`: true once `/api/health` answers `setup_mode: false`;
  - `type UpdateNoteItem = { id: string; text: string; addon: AddonId | null }`; `getUpdateNote(): Promise<{ note: { version: string; items: UpdateNoteItem[] } | null }>`; `dismissUpdateNote()`.
- Produces (`App.tsx`): `const [onSetup, setOnSetup] = useState(() => window.location.pathname === '/setup')`. While true it renders `<SetupPage onLeave={() => { history.replaceState(null, '', '/'); setOnSetup(false) }} />` before any session check.
- Produces (`SetupPage({ onLeave })`), using the sign-in card styles; the secret is read once, in a ref, on first render. States and copy (exact):
  - no secret → title "Set up Hearthkeep", text "Open setup from this PC: double-click Setup.cmd in the Hearthkeep folder."
  - `setupStatus` 404 → `onLeave()`; 403 or 429 → "This setup link has ended. Double-click Setup.cmd in the Hearthkeep folder to start again."
  - ready → title "Set up Hearthkeep", lede "One last step: create the passkey you'll use to open Hearthkeep on this PC.", primary button "Create passkey" (busy: "Waiting for your passkey…"). The click fetches the code with the secret and calls `enroll(code, guessDeviceName())` straight away. The code stays in a local variable: never in state, the DOM or the URL.
  - a passkey error → `passkeyErrorMessage(e, 'Setup')` as an alert, and the button again.
  - success → "Finishing setup…" (`role="status"`), then `waitForSetupDone()`: true → `window.location.replace('/')`; false → "Hearthkeep is taking a while to start. Open it from the Start menu in a minute."
- Produces (`RestartNotice`): `useRestart(): { restarting: boolean; error: string | null; restart(): Promise<void> }` does `stepUp('server')`, then `restartServer`, then `waitForRestart(info.started_at)`. True → `window.location.reload()`; false → error "Hearthkeep didn't come back. Open it from the Start menu on the PC." A 409 `not_supervised` → "Hearthkeep can't restart itself here. Stop it, then open it from the Start menu." Other failures → `passkeyErrorMessage(e, 'Restart')`. `RestartNotice()` renders nothing unless `info.restart_needed && info.supervised`; otherwise a note "A change needs Hearthkeep to restart." with a "Restart now" button (`aria-busy` while restarting).
- Produces (`ServerPanel`): `<section id="settings-general" tabIndex={-1}>` with `<h2>Hearthkeep</h2>`, "Version {version}", `RestartNotice`, "Restart Hearthkeep" (only when `supervised`), and "Stop Hearthkeep". Stop first asks in a confirmation dialog, "Stop Hearthkeep? It stops on this PC and on your other devices until you open it again from the Start menu on the PC.", with "Stop" and "Cancel"; then step-up, then `stopServer`; then the panel shows "Hearthkeep has stopped. Open it again from the Start menu on the PC." (`role="status"`).
- Produces (`UpdateNote`): loads once. With a note it renders `<section className="update-note" role="status" aria-labelledby=…>` with `<h2>Hearthkeep was updated</h2>`, then each item's text with a button "Open Settings › Add-ons" when `addon` is set (`openSettings(\`addon-${addon}\`)`), or "Nothing needs you." when there are no items, and a "Got it" button (`dismissUpdateNote`, then hidden). Load failures render nothing.

- [ ] **Step 1: Write the failing tests**

```tsx
// setup.test.tsx
it('reads the secret, clears it from the address bar, and creates the passkey with the fetched code', ...)
  // location '/setup#' + 'a'.repeat(43); mockFetch status 200, enrollment-code {code:'ABCDEFGH'}, enroll/options+verify, health {setup_mode:false}
  // after render: location.hash === '' and location.pathname === '/setup'
  // click "Create passkey" → enrollment-code body {secret: 'aaa…'}; enroll/options body {code: 'ABCDEFGH'};
  // no call path contains the secret or the code; screen never shows 'ABCDEFGH'; finally location.replace('/')
it('without a secret it says to double-click Setup.cmd', ...)
it('a 404 status leaves for the normal app', ...)             // onLeave called; App renders sign-in at '/'
it('a 403 says the link has ended', ...)
it('a cancelled passkey shows the message and the button again', ...)
it('never shows a command or placeholder', ...)                // no "hearthkeep ", "uv run", "--data", "<data>"

// server.test.tsx
it('restart asks for the passkey, then reloads when the server is back', ...)   // stepUp('server') → POST /api/server/restart {step_up_token}; second GET /api/server has a new started_at → reload
it('restart is hidden when Hearthkeep is not supervised', ...)
it('stop confirms, steps up, and says how to start again', ...)
it('restart notice shows in Settings › Add-ons and Hearthkeep only when needed', ...)
it('a step-up refusal shows a plain message and keeps running', ...)   // 403 step_up_required → passkey message; no reload
it('update note lists items with a button to the add-on and dismisses', ...)   // item sandbox_image → "Open Settings › Add-ons" → openSettings('addon-analysis'); "Got it" → POST dismiss; gone
it('update note with no items says nothing needs you', ...)
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd web && npx vitest run src/__tests__/setup.test.tsx src/__tests__/server.test.tsx`
Expected: FAIL (cannot resolve `../views/SetupPage`)

- [ ] **Step 3: Implement the clients, `SetupPage`, `RestartNotice`, `ServerPanel`, `UpdateNote` and the wiring** (dark tokens from `tokens.css`; reuse `.signin`, `.signin-card`, `.settings-section`, `.tile`, `.note`, `.btn`; no new CSS file unless the note needs one rule, then in `shell.css`)

- [ ] **Step 4: Run the web suite, the build and the server suite**

Run: `cd web && npm test && npm run build && cd ../server && uv run pytest -q`
Expected: PASS, build without type errors

- [ ] **Step 5: Commit**

```bash
git add web
git commit -m "feat(web): the setup page, stop and restart in Settings, and the update note"
```

---

## Self-review notes (spec against this plan)

- **§5.1 Entry point.** `Setup.cmd` installs a pinned, hash-checked uv per user with no admin rights, runs `uv run --project server hearthkeep setup`, explains each step and keeps the window open (Task 7). `hearthkeep setup` starts the server and opens the browser, the console says to keep the window open, and the server runs on afterwards (Tasks 3 and 6). The README tells users to double-click `Setup.cmd` (Task 7).
- **§5.2 Setup mode.**
  - It applies only before a passkey exists. Loopback only, the existing Origin and Host checks, and `/setup` is gone afterwards (Task 6).
  - The secret: a fragment link, cleared from the address bar, sent in the body, held only in memory, ended at the passkey or when the window closes; the link is printed only when the browser fails (Tasks 6 and 9).
  - The code is handed over in memory, never in a URL, log or audit (Tasks 3, 6 and 9).
  - The web bundle: a verified release bundle, Node on `main`, otherwise "main is for developers" with a link; how the version and the bundle are found is decided above and documented in CONTRIBUTING (Task 5). The README points at the release (Task 7).
- **§5.5 Lifecycle.** The server runs detached after setup and at normal start through the launcher (Tasks 3 and 6). Stop and restart from Settings › Hearthkeep and Settings › Add-ons behind step-up, audited, with restart-needed (Tasks 4 and 9). Start at login stays on Task Scheduler, now starting the launcher (Task 2). The Start-menu shortcut opens the app and starts the server if needed (Tasks 2 and 3).
- **§6a Upgrade.** A one-time, idempotent migration that records the data folder, moves the task, drops the placeholder rows when safe, leaves settings and cards alone, and is tested against copies of today's formats (Task 8). The one-time note (Tasks 8 and 9). Release-note text is project 7; install.md §14 says what the migration does.
- **§12.** Loopback, passkeys only, step-up, nothing personal (the repo slug is read from `origin`), downloads over HTTPS with SHA-256 (the bundle, the uv installer), no secrets in logs: all covered in Global Constraints and the tests named in Review Focus.
- **Owner update (Windows only).** One `Host` interface with a Windows implementation and `UnsupportedHost`. Other OSes report "unsupported" and never act (Tasks 1, 3, 6 and 8; Review Focus 5). CI stays on Linux.
- **Not in 2a:** the wizard screens (data folder choice, PC check, models, add-ons, remote access, downloads), the start-at-login switch, the advanced settings form, and building the sandbox from the app. They belong to 2b and 2c, which reuse `check_data_dir`, `ServerControl.mark_restart_needed`, `SetupMode` and the `/api/setup` gate.
