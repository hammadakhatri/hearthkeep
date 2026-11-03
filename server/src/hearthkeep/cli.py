"""Command line: `hearthkeep setup | serve | launch | open | enroll-code | secrets set/list |
sandbox build/status | startup enable/disable/status`.

`setup` (run by Setup.cmd) gets the web app ready, runs the server in setup mode until the
first passkey exists, then hands over to the launcher. `launch` keeps `serve` running in the
background (start at login runs it); `open` opens Hearthkeep in the browser, starting the
launcher if needed (the Start menu shortcut runs it)."""

from __future__ import annotations

import argparse
import dataclasses
import getpass
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

from hearthkeep import db, launcher, shortcut, startup
from hearthkeep.api.app import DB_FILE
from hearthkeep.api.common import ENROLLMENT_CODE_TTL_MINUTES
from hearthkeep.auth.enrollment import EnrollmentCodes, EnrollmentError, first_device_allowed
from hearthkeep.config import ConfigError, Settings, init_data_dir, load_settings
from hearthkeep.host import UNSUPPORTED_HOST, Host, current_host
from hearthkeep.lifecycle import EXIT_USAGE, SUPERVISED_ENV, ServerControl
from hearthkeep.providers.chat import NoChatModelClient, OpenAIChatClient
from hearthkeep.runtime.gpu import detect_vram_gb
from hearthkeep.runtime.supervisor import (
    API_KEY_FILE,
    LOOPBACK,
    ProcessError,
    build_comfyui_process,
    build_llama_process,
    ensure_chat_api_key,
)
from hearthkeep.sandbox import image as sandbox_image
from hearthkeep.secrets import SecretStore, SecretStoreError
from hearthkeep.setupmode import SetupMode, setup_url
from hearthkeep.webbundle import ensure_web_bundle

if TYPE_CHECKING:
    from fastapi import FastAPI

    from hearthkeep.auth.webauthn import Verifier
    from hearthkeep.models.cards import ModelCard
    from hearthkeep.runtime.stack import ModelStack
    from hearthkeep.tools.images import GenerateImageTool

# EXIT_USAGE (imported above) lives in lifecycle so the launcher can use it too.

GRACEFUL_SHUTDOWN_SECONDS = 5
STARTED_POLL_S = 0.1
STARTED_WAIT_S = 30.0
STARTUP_FAILURE = 3  # what uvicorn.run exits with when the server could not start

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
SHORTCUT_FAILED = (
    "The Start menu shortcut couldn't be made. You can still open Hearthkeep by "
    "double-clicking Setup.cmd."
)
FINISHED = (
    "Setup is finished. Hearthkeep keeps running in the background; open it from the Start "
    "menu any time. You can close this window now."
)
FINISHED_SLOW = (
    "Setup is finished, but Hearthkeep is taking a while to start. Open it from the Start "
    "menu in a minute. You can close this window now."
)
LOCATION_NOT_SAVED = (
    "Hearthkeep couldn't note where its data folder is on this PC. Setup carries on without it."
)

startup_run = subprocess.run  # how `startup` runs schtasks; replaced in tests


def _settings(data: Path) -> Settings:
    init_data_dir(data)
    return load_settings(data)


def _connect(settings: Settings):
    return db.connect(Path(settings.data_dir) / DB_FILE)


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


def _call_when_started(server, on_started: Callable[[], None]) -> None:
    """Call `on_started` once, from a daemon thread, when uvicorn reports it is serving."""

    def watch() -> None:
        deadline = time.monotonic() + STARTED_WAIT_S
        while time.monotonic() < deadline:
            if server.started:
                on_started()
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
) -> int:  # replaced in tests
    """Serve `app` until it is stopped; the exit code a restart or stop request asked for,
    else 0 (or STARTUP_FAILURE, as uvicorn.run, when the server never started)."""
    import uvicorn

    # proxy_headers=False: never derive the client address from X-Forwarded-For, so a local
    # process cannot rotate the rate-limit key by spoofing that header.
    # timeout_graceful_shutdown: open event streams never end on their own, so without a
    # bound a stop would hang until killed and skip the lifespan shutdown that closes turns.
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        proxy_headers=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = uvicorn.Server(config)
    if control is not None:
        control.attach(lambda: setattr(server, "should_exit", True))
    if on_started is not None:
        _call_when_started(server, on_started)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
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
    mode (otherwise, when config.toml names chat_base_url) keeps Phase 1 behaviour: that chat
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
    if rt.llama_server_exe is not None:
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
        name = settings.chat_api_key_secret
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
            "Chat: no chat model is set up yet. Open Hearthkeep and go to Settings › Add-ons."
        )
    if comfy is not None:
        scope = "" if owned else " Image models only make room for each other on the GPU."
        lines.append(
            f"Images: ComfyUI on {LOOPBACK}:{rt.comfyui_port}, started on first use.{scope}"
        )
    else:
        lines.append("Images: not set up. Open Settings › Add-ons to see what it needs.")

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
) -> int:
    """`make_llama`, `make_comfy` and `detect_vram` are seams for tests.

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
    host = "127.0.0.1" if settings.host == "localhost" else settings.host
    return run_server(app, host, settings.port, control=control)


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
    return any((path / name).exists() for name in ("config.toml", DB_FILE))


def _has_passkey(settings: Settings) -> bool:
    conn = _connect(settings)
    try:
        return not first_device_allowed(conn)
    finally:
        conn.close()


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

    # The folder: the one asked for, else the one setup recorded, else the default.
    recorded = host.read_location()
    data = Path(args.data) if args.data else (recorded or host.default_data_dir())
    # The folder rules are for a new folder; an existing install is used where it is.
    if not _is_data_folder(data):
        problem = host.check_data_dir(data)
        if problem:
            print(problem)
            return 1
    data = data.resolve()
    settings = _settings(data)  # creates the folder and its config on first run
    if recorded is None:  # never repoint a record whose folder still exists
        try:
            host.write_location(data)
        except OSError:
            print(LOCATION_NOT_SAVED)

    # Before the web app step: a re-run must not rebuild files a running Hearthkeep serves.
    if _has_passkey(settings):
        print(ALREADY_SET_UP)
        # In this window a problem is printed, not shown in a message box.
        return opener(settings, host, open_url=open_url, notify=print)

    print(STEP_WEB)
    if ensure_bundle(say=print) == "failed":
        return 1

    host_ip = "127.0.0.1" if settings.host == "localhost" else settings.host
    if not free(host_ip, settings.port):
        print(PORT_BUSY.format(port=settings.port))
        return 1

    print(STEP_START)
    if make_verifier is None:
        from hearthkeep.auth.webauthn import PyWebAuthnVerifier

        make_verifier = PyWebAuthnVerifier
    setup = SetupMode()
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

    def announce() -> None:
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

    run_server(app, host_ip, settings.port, control=control, on_started=announce)
    if not setup.completed:
        print(STOPPED_EARLY)
        return 1

    try:
        host.install_shortcut(data)
    except (shortcut.ShortcutError, OSError):
        print(SHORTCUT_FAILED)
    try:
        process = host.spawn_detached(launcher.launcher_argv(data))
    except OSError:
        process = None
    base = f"http://127.0.0.1:{settings.port}"
    print(FINISHED if wait(base, want_setup_mode=False, process=process) else FINISHED_SLOW)
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
    return launcher.open_app(_settings(Path(data)), host, open_url=open_url)


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
