"""Command line: `hearthkeep setup | serve | launch | open | enroll-code | secrets set/list |
sandbox build/status | startup enable/disable/status`.

`setup` (run by Setup.cmd) gets the web app ready, runs the server in setup mode until the
first passkey exists, then hands over to the launcher. `launch` keeps `serve` running in the
background (start at login runs it); `open` opens Hearthkeep in the browser, starting the
launcher if needed (the Start menu shortcut runs it)."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import functools
import getpass
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from uvicorn.config import STARTUP_FAILURE  # what uvicorn exits with when it could not start

import hearthkeep
from hearthkeep import db, launcher, shortcut, startup
from hearthkeep.api.app import DB_FILE
from hearthkeep.api.common import ENROLLMENT_CODE_TTL_MINUTES
from hearthkeep.api.routes_setup import folder_move_problem
from hearthkeep.auth.enrollment import EnrollmentCodes, EnrollmentError, first_device_allowed
from hearthkeep.config import (
    DATA_SUBDIRS,
    ConfigError,
    Settings,
    bind_address,
    init_data_dir,
    load_settings,
    local_base_url,
)
from hearthkeep.host import DATA_MARKERS, REPO_ROOT, UNSUPPORTED_HOST, Host, current_host
from hearthkeep.lifecycle import EXIT_RELOCATE, EXIT_USAGE, SUPERVISED_ENV, ServerControl
from hearthkeep.providers.chat import NoChatModelClient, OpenAIChatClient
from hearthkeep.push import VAPID_SECRET
from hearthkeep.runtime.gpu import detect_vram_gb
from hearthkeep.runtime.supervisor import (
    API_KEY_FILE,
    LLAMA_API_KEY_SECRET,
    LOOPBACK,
    ProcessError,
    build_comfyui_process,
    build_llama_process,
    ensure_chat_api_key,
)
from hearthkeep.sandbox import image as sandbox_image
from hearthkeep.secrets import SecretStore, SecretStoreError
from hearthkeep.setupmode import SetupMode, setup_url
from hearthkeep.shortcut import SHORTCUT_NOT_MADE as SHORTCUT_FAILED
from hearthkeep.upgrade import UpgradeReport, run_upgrade
from hearthkeep.webbundle import ensure_web_bundle, release_version

if TYPE_CHECKING:
    from fastapi import FastAPI

    from hearthkeep.auth.webauthn import Verifier
    from hearthkeep.models.cards import ModelCard
    from hearthkeep.runtime.stack import ModelStack
    from hearthkeep.tools.images import GenerateImageTool

# EXIT_USAGE (imported above) lives in lifecycle so the launcher can use it too.

GRACEFUL_SHUTDOWN_SECONDS = 5
STARTED_POLL_S = 0.1

NO_PASSKEY_YET = (
    "No passkey is set up yet. To finish setting up, double-click Setup.cmd in the "
    "Hearthkeep folder."
)
NOT_SET_UP = (
    "Hearthkeep isn't set up on this PC yet. Double-click Setup.cmd in the Hearthkeep folder."
)

# `hearthkeep setup` console copy: plain ASCII (the Windows console code page), no commands.
SETUP_INTRO = (
    "Setting up Hearthkeep\n=====================\nThis window sets up Hearthkeep on this PC."
)
STEP_WEB = "Step 2 of 3: Getting Hearthkeep's web app ready..."
STEP_START = "Step 3 of 3: Starting Hearthkeep..."
BROWSER_OPENING = "Your browser is opening Hearthkeep's setup page."
KEEP_OPEN = "Keep this window open until setup is finished. Closing it stops setup."
BROWSER_FAILED = (
    "Your browser didn't open. Open this link in your browser to continue setup. It works "
    "only while this window is open:"
)
ALREADY_SET_UP = "Hearthkeep is already set up on this PC. Opening it in your browser..."
PORT_BUSY = (
    "Another program on this PC is using Hearthkeep's address (port {port}). Close it, or "
    "restart the PC, then double-click Setup.cmd again."
)
STOPPED_EARLY = "Setup stopped before it was finished. Double-click Setup.cmd to start again."
FINISHED = (
    "Setup is finished. Hearthkeep keeps running in the background; open it from the Start "
    "menu any time. You can close this window now."
)
FINISHED_SLOW = (
    "Setup is finished, but Hearthkeep is taking a while to start. Open it from the Start "
    "menu in a minute. You can close this window now."
)
LAUNCHER_NOT_STARTED = (
    "Setup is finished, but Hearthkeep couldn't be started in the background. Double-click "
    "Setup.cmd again to start it."
)
LOCATION_NOT_SAVED = (
    "Hearthkeep couldn't note where its data folder is on this PC. Setup carries on without it."
)
MOVING_DATA = "Moving Hearthkeep's data to the folder you chose."
OLD_FOLDER_KEPT = "The old data folder was left in place. You can delete it yourself."
FOLDER_NOT_PROTECTED = (
    "Hearthkeep couldn't limit who on this PC can open its folders. Setup carries on without it."
)

startup_run = subprocess.run  # how `startup` runs schtasks; replaced in tests
log = logging.getLogger(__name__)


def _settings(data: Path) -> Settings:
    init_data_dir(data)
    return load_settings(data)


def _connect(settings: Settings):
    return db.connect(Path(settings.data_dir) / DB_FILE)


def _upgrade(settings: Settings, host: Host) -> UpgradeReport | None:
    """The one-time upgrade of an existing install, on its own connection. A failure is logged
    by type and never stops the start; None then."""
    try:
        conn = _connect(settings)
        try:
            return run_upgrade(Path(settings.data_dir).resolve(), conn, host=host, run=startup_run)
        finally:
            conn.close()
    except Exception as exc:
        log.warning("the upgrade didn't run: %s", type(exc).__name__)
        return None


def _print_code(code: str) -> None:
    print(
        f"\nFirst-device enrollment code: {code}\n"
        f"Open Hearthkeep in your browser and enter it within {ENROLLMENT_CODE_TTL_MINUTES} "
        "minutes to register your first passkey.\n"
    )


def build_registry(*, image_tool: GenerateImageTool | None = None):
    """The tools the agent may use. The image tool is registered only when it could be built
    (at least one image card and ComfyUI configured); the `images` level is listed either way."""
    from hearthkeep.tools.base import ToolRegistry

    registry = ToolRegistry()
    if image_tool is not None:
        registry.register(image_tool)
    return registry


def _call_when_started(server, on_started: Callable[[], None], finished: threading.Event) -> None:
    """Call `on_started` once, from a daemon thread, when uvicorn reports it is serving. It
    waits as long as the server is still starting (a slow first start must not leave setup
    silent), and gives up only when the server is exiting or has finished without starting."""

    def watch() -> None:
        while not finished.is_set():
            if server.started:
                on_started()
                return
            if server.should_exit:
                return
            time.sleep(STARTED_POLL_S)

    threading.Thread(target=watch, name="hearthkeep-started", daemon=True).start()


def run_server(
    app,
    host: str,
    port: int,
    *,
    control: ServerControl | None = None,
    on_started: Callable[[], None] | None = None,
    log_level: str = "info",
    access_log: bool = True,
) -> int:  # replaced in tests
    """Serve `app` until it is stopped; the exit code a restart or stop request asked for,
    else 0, or STARTUP_FAILURE when the server never started (uvicorn's own startup calls
    sys.exit with it when the bind or the app's startup fails). Setup passes a quieter
    `log_level` and no access log: its window is the owner's, not a log."""
    import uvicorn

    # proxy_headers=False: never derive the client address from X-Forwarded-For, so a local
    # process cannot rotate the rate-limit key by spoofing that header.
    # timeout_graceful_shutdown: open event streams never end on their own, so without a
    # bound a stop would hang until killed and skip the lifespan shutdown that closes turns.
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level,
        access_log=access_log,
        proxy_headers=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = uvicorn.Server(config)
    if control is not None:
        control.attach(lambda: setattr(server, "should_exit", True))
    finished = threading.Event()
    if on_started is not None:
        _call_when_started(server, on_started, finished)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    except SystemExit:
        if server.started:
            raise
        return STARTUP_FAILURE
    finally:
        finished.set()
    if control is not None and control.exit_code is not None:
        return control.exit_code
    return 0 if server.started else STARTUP_FAILURE


def _alias(card: ModelCard) -> str:
    """The model name llama-server answers to: the card's `--alias`, else its id."""
    args = card.launch_args
    for i, arg in enumerate(args):
        if arg == "--alias" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--alias="):
            return arg.split("=", 1)[1]
    return card.id


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def build_model_stack(
    settings: Settings,
    secrets: SecretStore,
    *,
    make_llama: Callable = build_llama_process,
    make_comfy: Callable = build_comfyui_process,
    detect_vram: Callable[[], float | None] = detect_vram_gb,
) -> tuple[ModelStack, OpenAIChatClient | NoChatModelClient, Settings, list[str]]:
    """The model stack, the chat client and the effective settings for `serve`, plus the
    startup lines to print (names only, never paths). Nothing is started here: each model
    server starts on its first lease.

    Owned mode (models.toml sets llama_server_exe and model_dirs for the default chat card):
    Hearthkeep runs llama-server itself and every chat stream leases the chat model. External
    mode (otherwise, or when models.toml says chat_server = "external", when config.toml names
    chat_base_url) keeps Phase 1 behaviour: that chat
    server is used as it is and stays out of the GPU scheduler. With neither, there is no chat
    model ("none"): the server still starts, and every chat stream says so. ComfyUI is managed
    when its three paths are set and at least one image card loaded.
    """
    from hearthkeep.models.cards import load_cards
    from hearthkeep.models.runtime import load_runtime
    from hearthkeep.providers.comfyui import ComfyUIClient
    from hearthkeep.runtime.gpu import ComfyBackend, LlamaBackend
    from hearthkeep.runtime.stack import ModelStack

    data = Path(settings.data_dir)
    rt = load_runtime(data)
    cards, errors = load_cards(data)
    managed: set[str] = set()
    backends: dict = {}
    processes: list = []

    chat_card = cards.get(rt.default_chat_model)
    owned = False
    # The owner chose their own chat server: installed local models stay where they are.
    external_chosen = rt.chat_server == "external" and settings.chat_base_url_set
    if rt.llama_server_exe is not None and not external_chosen:
        fallback = (
            "using the chat server at chat_base_url instead."
            if settings.chat_base_url_set
            else "Hearthkeep won't run llama-server."
        )
        if chat_card is None or chat_card.kind != "chat":
            _warn(
                f"llama_server_exe is set but default_chat_model {rt.default_chat_model} has "
                f"no chat card; {fallback}"
            )
        elif chat_card.id not in rt.model_dirs:
            _warn(
                f'llama_server_exe is set but model_dirs."{chat_card.id}" is not set in '
                f"models.toml; {fallback}"
            )
        else:
            owned = True
    if owned:
        chat_mode = "owned"
    elif settings.chat_base_url_set:
        chat_mode = "external"
    else:
        chat_mode = "none"

    chat_model_id = None
    if owned:
        name = LLAMA_API_KEY_SECRET  # never the external server's key
        key = ensure_chat_api_key(secrets, name)
        process = make_llama(
            chat_card,
            rt,
            data,
            data / API_KEY_FILE,
            api_key=lambda: ensure_chat_api_key(secrets, name),
        )
        base_url = f"http://{LOOPBACK}:{rt.llama_port}/v1"
        if settings.chat_base_url_set and settings.chat_base_url.rstrip("/") != base_url:
            _warn(
                "chat_base_url in config.toml differs from the llama-server Hearthkeep runs "
                f"(port {rt.llama_port} from models.toml); Hearthkeep uses its own server."
            )
        chat_client = OpenAIChatClient(base_url, key, _alias(chat_card))
        settings = dataclasses.replace(settings, chat_base_url=base_url, chat_model=chat_card.id)
        backends["llama_server"] = LlamaBackend(lambda card: process)
        processes.append(process)
        managed.add(chat_card.id)
        chat_model_id = chat_card.id
    elif chat_mode == "external":
        api_key = secrets.get(settings.chat_api_key_secret) or ""
        chat_client = OpenAIChatClient(settings.chat_base_url, api_key, settings.chat_model)
    else:
        chat_client = NoChatModelClient()

    comfy = None
    image_ids = {c.id for c in cards.values() if c.kind == "image"}
    if all((rt.comfyui_python, rt.comfyui_main, rt.comfyui_model_root)) and image_ids:
        comfy_process = make_comfy(rt, data)
        comfy = ComfyUIClient(f"http://{LOOPBACK}:{rt.comfyui_port}")
        backends["comfyui"] = ComfyBackend(comfy_process, comfy.free, comfy.vram_used)
        processes.append(comfy_process)
        managed |= image_ids

    def describe(card: ModelCard) -> str:
        if card.id in managed:
            return f"{card.id} ({card.kind}, managed)"
        if card.kind == "chat" and chat_mode == "external" and card.id == settings.chat_model:
            return f"{card.id} (chat, external server)"
        return f"{card.id} ({card.kind})"

    listed = ", ".join(describe(c) for c in cards.values()) or "none"
    lines = [f"Models: {listed}. Card problems: {len(errors)}."]
    lines += [f"Card problem in {e.id_or_file}: {e.message}" for e in errors]
    if owned:
        lines.append(
            f"Chat: Hearthkeep runs {chat_card.id} with llama-server on {LOOPBACK}:"
            f"{rt.llama_port}, started on first use."
        )
    elif chat_mode == "external":
        lines.append(
            "Chat: using the chat server at chat_base_url "
            "(Hearthkeep doesn't start or schedule it)."
        )
    else:
        lines.append(
            "Chat: no chat model is set up yet. Open Hearthkeep and go to Settings › Chat model."
        )
    if comfy is not None:
        scope = "" if owned else " Image models only make room for each other on the GPU."
        lines.append(
            f"Images: ComfyUI on {LOOPBACK}:{rt.comfyui_port}, started on first use.{scope}"
        )
    else:
        lines.append("Images: not set up. Open Settings › Image models to add them.")

    stack = ModelStack(
        cards=cards,
        card_errors=errors,
        managed=frozenset(managed),
        backends=backends,
        processes=processes,
        chat_model_id=chat_model_id,
        comfy=comfy,
        default_image_model=rt.default_image_model,
        gpu_vram_gb=rt.gpu_vram_gb,
        detect_vram=detect_vram,
        chat_mode=chat_mode,
        runtime=rt,
    )
    return stack, chat_client, settings, lines


def _log_to(path: Path) -> None:
    """Send everything printed from here on to `path` (appended, UTF-8, line by line). Started
    at login by pythonw there is no console, so without this the output would be lost."""
    path.parent.mkdir(parents=True, exist_ok=True)
    log = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 (lives as long as we do)
    sys.stdout = sys.stderr = log


def build_app(
    settings: Settings,
    *,
    verifier: Verifier,
    control: ServerControl,
    setup: SetupMode | None = None,
    make_llama: Callable = build_llama_process,
    make_comfy: Callable = build_comfyui_process,
    detect_vram: Callable[[], float | None] = detect_vram_gb,
) -> tuple[FastAPI, list[str]]:
    """The app `serve` runs, and the startup lines to print. Builds nothing that runs yet and
    never issues an enrollment code (only `cmd_serve` does, on a console). `setup` turns on
    setup mode. Raises ProcessError when a model server can't be set up."""
    from hearthkeep.api.app import create_app

    secrets = SecretStore(settings.data_dir)
    stack, chat_client, settings, lines = build_model_stack(
        settings, secrets, make_llama=make_llama, make_comfy=make_comfy, detect_vram=detect_vram
    )
    app = create_app(
        settings,
        verifier=verifier,
        chat_client=chat_client,
        registry=build_registry(),  # the image tool is added at startup when it can be built
        secrets=secrets,
        models=stack,
        control=control,
        setup=setup,
    )
    return app, lines


def _interactive() -> bool:
    """Whether the output is a console someone is looking at (never under pythonw)."""
    return sys.stdout is not None and sys.stdout.isatty()


def cmd_serve(
    args: argparse.Namespace,
    *,
    make_llama: Callable = build_llama_process,
    make_comfy: Callable = build_comfyui_process,
    detect_vram: Callable[[], float | None] = detect_vram_gb,
    host: Host | None = None,
) -> int:
    """`make_llama`, `make_comfy`, `detect_vram` and `host` are seams for tests.

    While no passkey exists the first-device code is printed only to an interactive console
    without a log file; otherwise `serve` says to run Setup.cmd and issues no code. With a log
    file (the launcher, start at login) it then stops with EXIT_USAGE before starting, so no
    background server holds the port that setup needs."""
    from hearthkeep.auth.insecure_dev import (
        WARNING,
        InsecureDevAuthError,
        InsecureDevVerifier,
        check_allowed,
    )
    from hearthkeep.auth.webauthn import PyWebAuthnVerifier

    if args.log_file:
        _log_to(Path(args.log_file))
    settings = _settings(Path(args.data))
    _upgrade(settings, host or current_host())
    if args.insecure_dev_auth:
        conn = _connect(settings)
        try:
            check_allowed(settings, conn)
        except InsecureDevAuthError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        finally:
            conn.close()
        print(WARNING, file=sys.stderr)
        verifier = InsecureDevVerifier()
    else:
        verifier = PyWebAuthnVerifier(settings)

    conn = _connect(settings)
    try:
        no_passkey = first_device_allowed(conn)
    finally:
        conn.close()
    show_code = no_passkey and not args.log_file and _interactive()
    if no_passkey and not show_code:
        print(NO_PASSKEY_YET)
        if args.log_file:  # in the background: stop rather than hold the port
            return EXIT_USAGE

    control = ServerControl(supervised=os.environ.get(SUPERVISED_ENV) == "1")
    try:
        app, lines = build_app(
            settings, verifier=verifier, control=control, make_llama=make_llama,
            make_comfy=make_comfy, detect_vram=detect_vram,
        )  # fmt: skip
    except ProcessError as exc:
        print(f"error: {exc.user_message}", file=sys.stderr)
        return EXIT_USAGE
    print("\n".join(lines))

    if show_code:
        conn = _connect(settings)
        try:
            _print_code(EnrollmentCodes(conn).issue("first_device", ENROLLMENT_CODE_TTL_MINUTES))
        finally:
            conn.close()

    # Never bind anything but the loopback interface. On Ctrl+C uvicorn runs the app's
    # shutdown, which stops every model server Hearthkeep started.
    return run_server(app, bind_address(settings.host), settings.port, control=control)


def port_free(host: str, port: int) -> bool:
    """Whether `port` can be bound on `host` now: a bind test on a socket closed at once."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _is_data_folder(path: Path) -> bool:
    """Whether `path` already holds a Hearthkeep data folder (an install to keep as it is)."""
    return any((path / name).exists() for name in DATA_MARKERS)


def _release_program_folder(
    *, root: Path = REPO_ROOT, version: str = hearthkeep.__version__
) -> Path | None:
    """The Hearthkeep program folder when it is an unzipped release, else None. A developer's
    checkout is left alone: a development version, or any git clone (a clone of a release tag
    has a release version too, and holds node_modules and the developer's own files)."""
    if release_version(version) is None or (root / ".git").exists():
        return None
    return root


# What setup itself puts in a new data folder: the files below (each with the check that it
# is still only setup's own), and the empty folders DATA_SUBDIRS names with the folders that
# hold them. Starting the setup server adds two: git's empty settings file, and the secret
# store holding keys Hearthkeep made itself (a key or token the owner typed keeps the folder).
SETUP_SECRETS = frozenset({VAPID_SECRET, LLAMA_API_KEY_SECRET})


def _setup_secrets_only(folder: Path) -> bool:
    try:
        return set(SecretStore(folder).names()) <= SETUP_SECRETS
    except (SecretStoreError, OSError):
        return False


SETUP_FILES: dict[str, Callable[[Path], bool]] = {
    "config.toml": lambda folder: True,
    DB_FILE: lambda folder: True,
    f"{DB_FILE}-wal": lambda folder: True,
    f"{DB_FILE}-shm": lambda folder: True,
    "runtime/git-home/empty.gitconfig": (
        lambda folder: (folder / "runtime/git-home/empty.gitconfig").stat().st_size == 0
    ),
    "secrets/store.bin": _setup_secrets_only,
}
SETUP_DIRS = frozenset(
    "/".join(sub.split("/")[:n]) for sub in DATA_SUBDIRS for n in range(1, sub.count("/") + 2)
)


def _setup_folder_contents(folder: Path) -> list[Path] | None:
    """Everything in `folder`, files first and then folders deepest first, when all of it is
    setup's own (SETUP_FILES that pass their check, in SETUP_DIRS that hold nothing else);
    None when anything else is there, a link included."""
    files: list[Path] = []
    dirs: list[Path] = []

    def walk(directory: Path, rel: str) -> bool:
        with os.scandir(directory) as entries:
            found = list(entries)
        for entry in found:
            name = f"{rel}/{entry.name}" if rel else entry.name
            if entry.is_symlink() or entry.is_junction():
                return False
            if entry.is_dir(follow_symlinks=False):
                if name not in SETUP_DIRS or not walk(Path(entry.path), name):
                    return False
                dirs.append(Path(entry.path))  # after what is inside it
            elif entry.is_file(follow_symlinks=False) and name in SETUP_FILES:
                files.append(Path(entry.path))
            else:
                return False
        return True

    try:
        if not walk(folder, ""):
            return None
        found = {path.relative_to(folder).as_posix() for path in files}
        if not all(SETUP_FILES[name](folder) for name in found):
            return None
    except OSError:
        return None
    return files + dirs


def _removable(path: Path) -> bool:
    """Whether the file `path` can be deleted now: it is renamed aside and straight back
    (Windows refuses that, as it refuses the delete, while another program has it open)."""
    probe = path.with_name(path.name + ".hk-probe")
    try:
        os.rename(path, probe)
    except OSError:
        return False
    try:
        os.rename(probe, path)
    except OSError as exc:
        log.warning("a file in the old data folder couldn't be put back: %s", type(exc).__name__)
        return False
    return True


def _remove_setup_folder(folder: Path, *, keep_folder: bool = False) -> bool:
    """Take setup's own files out of a data folder this run is moving away from, only when it
    holds nothing else (an owner's file anywhere in it keeps all of it), and then the folder
    itself unless `keep_folder` (it was there before this run). A dry pass first: when any
    file can't be deleted (another program has it open) nothing at all is removed. The secret
    store goes first, with its master key in the OS keyring. Whether it was all done. When the
    keyring can't forget that key, the store file is already gone, its key stays in the
    keyring and every other file stays; any other failure part way leaves what was not yet
    removed. Failures are logged by type only."""
    if folder.is_symlink() or not folder.is_dir():
        return False
    contents = _setup_folder_contents(folder)
    if contents is None:
        return False
    if not all(_removable(path) for path in contents if not path.is_dir()):
        return False
    store = folder / "secrets" / "store.bin"
    try:
        if store in contents:
            SecretStore(folder).forget()  # the file, then its key
        for path in contents:
            if path.is_dir():
                path.rmdir()  # never a folder with anything still in it
            else:
                path.unlink(missing_ok=True)
        if not keep_folder:
            folder.rmdir()
    except (OSError, SecretStoreError) as exc:
        log.warning("the old data folder couldn't be removed: %s", type(exc).__name__)
        return False
    return True


def _protect_folder(host: Host, path: Path) -> None:
    """Let only the owner's account, SYSTEM and Administrators into `path` (ruling C24). A
    failure is logged by type and never stops setup."""
    try:
        host.protect_folder(path)
    except Exception as exc:
        log.warning("a folder couldn't be protected: %s", type(exc).__name__)
        print(FOLDER_NOT_PROTECTED)


def _reset_folder(host: Host, path: Path) -> None:
    """Give a kept folder back its inherited permissions. A failure is logged by type and
    never stops setup (the folder stays limited to the owner, SYSTEM and Administrators)."""
    try:
        host.reset_folder_permissions(path)
    except Exception as exc:
        log.warning("a folder's permissions couldn't be reset: %s", type(exc).__name__)


def _has_passkey(settings: Settings) -> bool:
    conn = _connect(settings)
    try:
        return not first_device_allowed(conn)
    finally:
        conn.close()


def _install_shortcut(host: Host, data: Path) -> None:
    try:
        host.install_shortcut(data)
    except (shortcut.ShortcutError, OSError):
        print(SHORTCUT_FAILED)


def _task_data_folder(host: Host) -> Path | None:
    """The data folder the start-at-login task runs, when it is a Hearthkeep data folder; None
    when there is no task or it can't be read (the non-strict check: nothing here changes)."""
    try:
        state = host.startup_status(run=startup_run)
    except Exception as exc:
        log.warning("the start-at-login task couldn't be read: %s", type(exc).__name__)
        return None
    if not state.data_dir:
        return None
    folder = Path(state.data_dir)
    return folder if _is_data_folder(folder) else None


@contextlib.contextmanager
def _quiet_logs():
    """Keep Hearthkeep's own log lines out of the setup window: with no logging set up, a
    warning would reach the console through logging.lastResort. Put back as it was after."""
    logger = logging.getLogger("hearthkeep")
    quiet = logging.NullHandler()
    propagate = logger.propagate
    logger.addHandler(quiet)
    logger.propagate = False
    try:
        yield
    finally:
        logger.removeHandler(quiet)
        logger.propagate = propagate


def _in_quiet_window(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _quiet_logs():
            return func(*args, **kwargs)

    return wrapper


@_in_quiet_window
def cmd_setup(
    args: argparse.Namespace,
    *,
    host: Host | None = None,
    open_url: Callable[[str], bool] = webbrowser.open,
    ensure_bundle: Callable = ensure_web_bundle,
    make_verifier: Callable[[Settings], Verifier] | None = None,
    wait: Callable = launcher.wait_for_health,
    opener: Callable = launcher.open_app,
    free: Callable[[str, int], bool] = port_free,
    program_folder: Callable[[], Path | None] = _release_program_folder,
    make_llama: Callable = build_llama_process,
    make_comfy: Callable = build_comfyui_process,
    detect_vram: Callable[[], float | None] = detect_vram_gb,
) -> int:
    """Set Hearthkeep up on this PC (Setup.cmd runs this; the arguments after `args` are seams
    for tests). A re-run once a passkey exists only opens the app. Otherwise it gets the web app
    ready, serves in setup mode until the first passkey is made in the browser, then installs
    the Start menu shortcut and starts the launcher in the background."""
    print(SETUP_INTRO)
    host = host or current_host()
    if not host.supported:
        print(UNSUPPORTED_HOST)
        return EXIT_USAGE

    # The folder: the one asked for, else the one setup recorded, else the one start at login
    # runs (an install whose record is gone), else the default.
    recorded = host.read_location()
    if args.data:
        data = Path(args.data)
    else:
        data = recorded or _task_data_folder(host) or host.default_data_dir()
    # The folder rules are for a new folder. An existing Hearthkeep data folder skips all of
    # check_data_dir (controller ruling on C2): existing installs must keep working as they are.
    new_folder = not _is_data_folder(data)
    if new_folder:
        problem = host.check_data_dir(data)
        if problem:
            print(problem)
            return 1
    data = data.resolve()
    if make_verifier is None:
        from hearthkeep.auth.webauthn import PyWebAuthnVerifier

        make_verifier = PyWebAuthnVerifier
    # One setup run, one secret: a move to another folder keeps the page that is open.
    setup = SetupMode()
    # Never repoint a record whose folder still exists: the record is written when there is
    # none. After a move it is repointed only when it named the folder being moved (this run
    # made that folder), never when it names another install.
    write_record = recorded is None
    record_follows_moves = recorded is None or Path(recorded).resolve() == data
    moved = False
    while True:
        setup.created_folder = new_folder
        setup.folder_existed = data.exists()
        # Protected before Hearthkeep writes anything in it. A folder that already has its
        # database is an install in use and is kept as it is (C2); one with only a config.toml
        # the owner made first (for Tailscale) holds nothing of theirs yet, so it is protected.
        if not (data / DB_FILE).exists():
            data.mkdir(parents=True, exist_ok=True)
            _protect_folder(host, data)
        if not moved:
            program = program_folder()
            if program is not None:
                _protect_folder(host, program)
        settings = _settings(data)  # creates the folder's files and its config on first run
        if write_record:
            try:
                host.write_location(data)
            except OSError:
                print(LOCATION_NOT_SAVED)
        report = _upgrade(settings, host)

        # Before the web app step: a re-run must not rebuild files a running Hearthkeep serves.
        if _has_passkey(settings):
            print(ALREADY_SET_UP)
            # Make the Start menu shortcut again (it may have failed the first time), unless
            # the upgrade just made it.
            if report is None or "shortcut" not in report.changed:
                _install_shortcut(host, data)
            # In this window a problem is printed, not shown in a message box.
            return opener(settings, host, open_url=open_url, notify=print)

        if not moved:  # the web app is the program's, not the data folder's: made once
            print(STEP_WEB)
            if ensure_bundle(say=print) == "failed":
                return 1

        host_ip = bind_address(settings.host)
        if not free(host_ip, settings.port):
            print(PORT_BUSY.format(port=settings.port))
            return 1

        if not moved:
            print(STEP_START)
        control = ServerControl(supervised=False, setup_mode=True)
        try:
            # Its startup lines are not printed: model details mean nothing at first setup.
            app, _lines = build_app(
                settings, verifier=make_verifier(settings), control=control, setup=setup,
                make_llama=make_llama, make_comfy=make_comfy, detect_vram=detect_vram,
            )  # fmt: skip
        except ProcessError as exc:
            print(exc.user_message)
            return 1

        def announce(settings: Settings = settings) -> None:
            secret = setup.secret
            if secret is None:
                return
            link = setup_url(settings.public_origin, secret)
            try:
                opened = bool(open_url(link))
            except Exception:  # no browser at all: the same as one that didn't open
                opened = False
            if opened:
                print(BROWSER_OPENING)
                print(KEEP_OPEN)
            else:
                # The only time the link (and so the secret) is shown: in this window alone.
                print(BROWSER_FAILED)
                print(link)

        # After a move the setup page is already open: it finds the server again by itself.
        code = run_server(
            app, host_ip, settings.port, control=control,
            on_started=None if moved else announce, log_level="critical", access_log=False,
        )  # fmt: skip
        target = setup.next_data_dir
        if code == EXIT_RELOCATE and target is not None and not setup.completed:
            setup.next_data_dir = None
            # The page checked it; checked again, as the folder may have changed since.
            problem = folder_move_problem(host, target, data)
            if problem:
                print(problem)
                print(STOPPED_EARLY)
                return 1
            print(MOVING_DATA)
            if setup.created_folder:
                # Only when nothing but setup's own files is there; a folder that was there
                # before this run is emptied of them and kept.
                if not _remove_setup_folder(data, keep_folder=setup.folder_existed):
                    print(OLD_FOLDER_KEPT)
                elif setup.folder_existed:
                    _reset_folder(host, data)
            data, new_folder, moved = target.resolve(), True, True
            write_record = record_follows_moves
            continue
        break

    if not setup.completed:
        # A start that failed with the port now taken: something took it after the check
        # above. Any other failure (the app's own startup, say) is not a busy port.
        busy = code == STARTUP_FAILURE and not free(host_ip, settings.port)
        print(PORT_BUSY.format(port=settings.port) if busy else STOPPED_EARLY)
        return 1

    _install_shortcut(host, data)
    try:
        process = host.spawn_detached(launcher.launcher_argv(data))
    except OSError:
        print(LAUNCHER_NOT_STARTED)
        return 1
    if wait(local_base_url(settings), want_setup_mode=False, process=process):
        print(FINISHED)
    elif launcher.launcher_ended(process) is not None:
        print(LAUNCHER_NOT_STARTED)  # the launcher stopped: nothing will start on its own
        return 1
    else:
        print(FINISHED_SLOW)
    return 0


def _host_data(args: argparse.Namespace, host: Host) -> Path | None:
    """The data folder for `launch` and `open`, resolved: `--data`, else the one setup
    recorded."""
    data = Path(args.data) if args.data else host.read_location()
    return data.resolve() if data is not None else None


def cmd_launch(
    args: argparse.Namespace, *, host: Host | None = None, popen=subprocess.Popen
) -> int:
    host = host or current_host()
    if not host.supported:
        print(UNSUPPORTED_HOST, file=sys.stderr)
        return EXIT_USAGE
    data = _host_data(args, host)
    if data is None:
        print(NOT_SET_UP, file=sys.stderr)
        return 1
    return launcher.run_launcher(data, popen=popen)


def cmd_open(
    args: argparse.Namespace, *, host: Host | None = None, open_url=webbrowser.open
) -> int:
    host = host or current_host()
    if not host.supported:
        print(UNSUPPORTED_HOST, file=sys.stderr)
        return EXIT_USAGE
    data = _host_data(args, host)
    if data is None:
        print(NOT_SET_UP, file=sys.stderr)
        return 1
    # Run under pythonw from the Start menu: no console, so nothing printed is ever seen. A
    # problem is shown in a message box instead of being dropped.
    try:
        return launcher.open_app(_settings(Path(data)), host, open_url=open_url)
    except Exception as exc:
        log.warning("open failed: %s", type(exc).__name__)
        launcher.show_message(launcher.LAUNCHER_NOT_READY)
        return EXIT_USAGE


def cmd_enroll_code(args: argparse.Namespace) -> int:
    settings = _settings(Path(args.data))
    conn = _connect(settings)
    try:
        code = EnrollmentCodes(conn).issue("first_device", ENROLLMENT_CODE_TTL_MINUTES)
    except EnrollmentError:
        print(
            "error: a passkey is already enrolled. This command only creates the code for "
            "the first device.",
            file=sys.stderr,
        )
        return 1
    finally:
        conn.close()
    _print_code(code)
    return 0


def _read_secret_value(name: str) -> str:
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(f"Value for '{name}' (input hidden): ")
    return sys.stdin.read().rstrip("\r\n")


def cmd_secrets_set(args: argparse.Namespace) -> int:
    settings = _settings(Path(args.data))
    store = SecretStore(settings.data_dir)
    value = _read_secret_value(args.name)
    if not value:
        print("error: empty value, nothing stored", file=sys.stderr)
        return 1
    try:
        store.put(args.name, value)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"Stored secret '{args.name}'.")
    return 0


def cmd_secrets_list(args: argparse.Namespace) -> int:
    settings = _settings(Path(args.data))
    for name in SecretStore(settings.data_dir).names():
        print(name)
    return 0


SANDBOX_BUILD_INTRO = (
    "Building the Python sandbox image. The first build downloads Python and the analysis "
    "libraries (about 1 GB)."
)


def _sandbox_images() -> list[tuple[str, str, Path]]:
    """(label, tag, folder) for each image `sandbox build` builds, in order."""
    return [
        ("Sandbox image", sandbox_image.image_tag(), sandbox_image.IMAGE_DIR),
        ("Repo image", sandbox_image.repo_image_tag(), sandbox_image.REPO_IMAGE_DIR),
    ]


def cmd_sandbox_build(args: argparse.Namespace) -> int:
    _settings(Path(args.data))
    print(SANDBOX_BUILD_INTRO, flush=True)
    if not sandbox_image.docker_ok(sandbox_image.DOCKER):
        print(
            "error: Docker is not running or not installed. Start Docker Desktop and try again.",
            file=sys.stderr,
        )
        return 1
    for label, tag, folder in _sandbox_images():
        code = sandbox_image.build_image(tag, sandbox_image.DOCKER, folder)
        if code != 0:
            print(
                f"error: the {label.lower()} build failed (docker exit code {code}).",
                file=sys.stderr,
            )
            return 1
        print(f"{label} ready: {tag}", flush=True)
    return 0


def cmd_sandbox_status(args: argparse.Namespace) -> int:
    _settings(Path(args.data))
    docker = sandbox_image.DOCKER
    running = sandbox_image.docker_ok(docker)
    print(f"Docker: {'running' if running else 'not found'}")
    for label, tag, _folder in _sandbox_images():
        present = running and sandbox_image.image_present(tag, docker)
        print(f"{label}: {tag} ({'present' if present else 'missing'})")
    return 0


def _startup_unsupported() -> int:
    print(startup.UNSUPPORTED, file=sys.stderr)
    return EXIT_USAGE


def cmd_startup_enable(args: argparse.Namespace) -> int:
    if not startup.is_windows():
        return _startup_unsupported()
    settings = _settings(Path(args.data))
    data = Path(settings.data_dir).resolve()
    try:
        startup.enable(data, run=startup_run)
    except startup.StartupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Hearthkeep will start when you sign in to Windows. Logs: {startup.log_path(data)}")
    return 0


def cmd_startup_disable(args: argparse.Namespace) -> int:
    if not startup.is_windows():
        return _startup_unsupported()
    try:
        removed = startup.disable(run=startup_run)
    except startup.StartupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("Start at login: off" + ("" if removed else " (it was not on)"))
    return 0


def cmd_startup_status(args: argparse.Namespace) -> int:
    if not startup.is_windows():
        return _startup_unsupported()
    state = startup.status(run=startup_run)
    data = Path(_settings(Path(args.data)).data_dir)
    if startup.on_for_another_folder(state, data):
        print(f"Start at login: on, for another data folder ({state.data_dir})")
    elif state.enabled:
        print(f"Start at login: on (data folder {state.data_dir})")
    else:
        print("Start at login: off")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hearthkeep", description="Your private AI, at home.")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_data(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--data", required=True, help="path to your Hearthkeep data folder")
        return p

    serve = with_data(sub.add_parser("serve", help="run the Hearthkeep server"))
    serve.add_argument(
        "--insecure-dev-auth",
        action="store_true",
        help="DEVELOPMENT ONLY: accept any passkey response (localhost origins only)",
    )
    serve.add_argument(
        "--log-file", metavar="PATH", help="append everything printed to PATH (UTF-8)"
    )
    serve.set_defaults(func=cmd_serve)

    setup = sub.add_parser("setup", help="set up Hearthkeep on this PC (used by Setup.cmd)")
    setup.add_argument("--data", help="path to your Hearthkeep data folder")
    setup.set_defaults(func=cmd_setup)

    launch = sub.add_parser(
        "launch", help="keep Hearthkeep running in the background (used by start at login)"
    )
    launch.add_argument("--data", help="path to your Hearthkeep data folder")
    launch.set_defaults(func=cmd_launch)
    open_ = sub.add_parser("open", help="open Hearthkeep in the browser, starting it if needed")
    open_.add_argument("--data", help="path to your Hearthkeep data folder")
    open_.set_defaults(func=cmd_open)

    enroll = with_data(sub.add_parser("enroll-code", help="issue a first-device enrollment code"))
    enroll.set_defaults(func=cmd_enroll_code)

    secrets = sub.add_parser("secrets", help="manage encrypted secrets")
    secrets_sub = secrets.add_subparsers(dest="secrets_command", required=True)
    put = with_data(secrets_sub.add_parser("set", help="store a secret (value is never echoed)"))
    put.add_argument("name")
    put.set_defaults(func=cmd_secrets_set)
    names = with_data(secrets_sub.add_parser("list", help="list secret names (not values)"))
    names.set_defaults(func=cmd_secrets_list)

    sandbox = sub.add_parser("sandbox", help="the Python sandbox image (needs Docker)")
    sandbox_sub = sandbox.add_subparsers(dest="sandbox_command", required=True)
    build = with_data(sandbox_sub.add_parser("build", help="build the sandbox image (one time)"))
    build.set_defaults(func=cmd_sandbox_build)
    status = with_data(sandbox_sub.add_parser("status", help="show Docker and the sandbox image"))
    status.set_defaults(func=cmd_sandbox_status)

    login = sub.add_parser("startup", help="start Hearthkeep when you sign in (Windows)")
    login_sub = login.add_subparsers(dest="startup_command", required=True)
    for name, func, text in (
        ("enable", cmd_startup_enable, "start Hearthkeep, with no window, when you sign in"),
        ("disable", cmd_startup_disable, "stop starting Hearthkeep when you sign in"),
        ("status", cmd_startup_status, "show whether Hearthkeep starts when you sign in"),
    ):
        with_data(login_sub.add_parser(name, help=text)).set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SecretStoreError as exc:
        print(f"secret store error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
