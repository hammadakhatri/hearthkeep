"""The add-on registry: one place that knows whether chat and each add-on (analysis, images,
GitHub, phone and devices, start at login) can be used right now, and what to say when not.

The checks that touch the PC (files, Docker, Tailscale, Task Scheduler) run in a background
refresh, off the event loop, and are cached. `get()` reads that cache; only chat (the GPU's
loading state) and GitHub (Git and the saved token) are worked out at call time. Nothing here
carries a file path, a token or the tailnet name, and a failing check logs its type only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from hearthkeep import startup
from hearthkeep.config import LOOPBACK_HOSTS, Settings
from hearthkeep.models.runtime import card_file_paths
from hearthkeep.procutil import NO_WINDOW, find_executable
from hearthkeep.sandbox.runner import SandboxStatus
from hearthkeep.startup import StartupStatus

if TYPE_CHECKING:
    from hearthkeep.github.repos import RepoStore
    from hearthkeep.runtime.gpu import GpuScheduler
    from hearthkeep.runtime.stack import ModelStack
    from hearthkeep.sandbox.runner import SandboxRunner

log = logging.getLogger(__name__)

AddonId = Literal["chat", "analysis", "images", "github", "devices", "startup"]
ADDON_IDS: tuple[AddonId, ...] = ("chat", "analysis", "images", "github", "devices", "startup")
REFRESH_SECONDS = 30.0
TAILSCALE_TIMEOUT_S = 5

# The add-ons a tool needs; a tool not listed needs none.
TOOL_ADDONS: dict[str, tuple[AddonId, ...]] = {
    "python": ("analysis",),
    "read_pages": ("analysis",),
    "files": ("analysis",),
    "generate_image": ("images",),
    "repo_read": ("github",),
    "repo_edit": ("github",),
    "repo_install": ("github",),
    "github": ("github",),
    "repo_test": ("github", "analysis"),
}
# The add-ons behind each tool level (Tools & permissions).
LEVEL_ADDONS: dict[str, tuple[AddonId, ...]] = {
    "python": ("analysis",),
    "files": ("analysis",),
    "images": ("images",),
    "github": ("github",),
    "github_tests": ("github", "analysis"),
}

# id -> (name, about, requires)
ABOUT: dict[AddonId, tuple[str, str, str]] = {
    "chat": (
        "Chat model",
        "Chat with a model that runs on this PC.",
        "llama.cpp and a chat model, or a chat server you already run on this PC.",
    ),
    "analysis": (
        "Analysis",
        "Python analysis of your files, and reading PDFs and scans.",
        "Docker Desktop, and the analysis sandbox (built once, about 1 GB).",
    ),
    "images": (
        "Images",
        "Create and edit images on this PC.",
        "ComfyUI and at least one image model.",
    ),
    "github": (
        "GitHub",
        "Work on your GitHub repos: read, edit, test and open pull requests.",
        "Git 2.31 or newer and a GitHub token. Running tests also needs Analysis.",
    ),
    "devices": (
        "Phone and devices",
        "Use Hearthkeep from your phone and other devices.",
        "Tailscale, signed in, with HTTPS turned on.",
    ),
    "startup": (
        "Start at login",
        "Hearthkeep starts on its own when you sign in.",
        "Windows.",
    ),
}

_READY = "Ready."
# (id, state) -> (message, action, short)
_COPY: dict[tuple[AddonId, str], tuple[str, str | None, str | None]] = {
    ("chat", "not_set_up"): (
        "Set up your chat model to start chatting.",
        "open_setup",
        "Needs a chat model",
    ),
    ("chat", "ready"): (_READY, None, None),
    ("chat", "starting"): ("Loading the chat model…", None, None),
    ("chat", "offline"): (
        "The chat server isn't answering. Make sure it's running on this PC.",
        "open_setup",
        "Chat server offline",
    ),
    ("chat", "error"): (
        "Hearthkeep can't find the chat model's files on this PC.",
        "open_setup",
        "Chat model files missing",
    ),
    ("analysis", "not_set_up"): (
        "Docker Desktop isn't installed on this PC.",
        "install",
        "Needs Docker",
    ),
    ("analysis", "docker_stopped"): (
        "Docker Desktop isn't running. Start it, then check again.",
        "start_docker",
        "Needs Docker",
    ),
    ("analysis", "needs_image"): (
        "The analysis sandbox isn't built yet.",
        "build_image",
        "Needs the analysis sandbox",
    ),
    ("analysis", "ready"): (_READY, None, None),
    ("images", "not_set_up"): (
        "ComfyUI and an image model aren't set up on this PC.",
        "install",
        "Needs Images",
    ),
    ("images", "error"): (
        "Hearthkeep can't find ComfyUI or the image model files on this PC.",
        "open_setup",
        "Needs Images",
    ),
    ("images", "ready"): (_READY, None, None),
    ("github", "not_set_up"): (
        "Git isn't installed and no GitHub token is saved.",
        "install",
        "Needs Git and a token",
    ),
    ("github", "needs_git"): (
        "Git 2.31 or newer isn't installed on this PC.",
        "install",
        "Needs Git",
    ),
    ("github", "needs_token"): (
        "Save a GitHub token to let Hearthkeep work on your repos.",
        "add_token",
        "Needs a GitHub token",
    ),
    ("github", "ready"): (_READY, None, None),
    ("devices", "this_pc_only"): ("Hearthkeep only answers on this PC.", "open_setup", None),
    ("devices", "needs_tailscale"): (
        "Remote access is on, but Tailscale isn't running or signed in on this PC.",
        "open_setup",
        None,
    ),
    ("devices", "ready"): ("Your phone and other devices can reach Hearthkeep.", None, None),
    ("startup", "off"): (
        "Hearthkeep doesn't start on its own when you sign in.",
        "turn_on",
        None,
    ),
    ("startup", "on"): ("Hearthkeep starts when you sign in to Windows.", None, None),
    ("startup", "unsupported"): ("Start at login is built in only on Windows.", None, None),
}
# Messages that replace a state's usual one in a particular case.
MESSAGE_VARIANTS: dict[str, str] = {
    "chat.ready_not_loaded": "Ready. It loads when you send a message.",
    "startup.off_other_folder": "Start at login is on for another data folder.",
}
_READY_STATES = frozenset({"ready", "on"})


@dataclass(frozen=True)
class AddonStatus:
    id: AddonId
    state: str
    message: str
    action: str | None
    short: str | None
    details: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.state in _READY_STATES

    def to_json(self) -> dict:
        name, about, requires = ABOUT[self.id]
        return {
            "id": self.id,
            "name": name,
            "about": about,
            "requires": requires,
            "state": self.state,
            "ready": self.ready,
            "message": self.message,
            "action": self.action,
            "short": self.short,
            "details": dict(self.details),
        }


def _status(
    addon_id: AddonId, state: str, *, message: str | None = None, **details: Any
) -> AddonStatus:
    text, action, short = _COPY[(addon_id, state)]
    return AddonStatus(addon_id, state, message or text, action, short, details)


# --- probes that touch the PC ----------------------------------------------------------------


@dataclass(frozen=True)
class TailscaleState:
    installed: bool
    running: bool
    dns_name: str | None


def _tailscale_fallbacks() -> list[Path]:
    program_files = os.environ.get("ProgramFiles")
    return [Path(program_files) / "Tailscale" / "tailscale.exe"] if program_files else []


def tailscale_state(
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> TailscaleState:
    """Whether Tailscale is installed, running and signed in, and this PC's tailnet name
    (lowercase, no trailing dot). Only reads `tailscale status --json`; never raises."""
    exe = find_executable("tailscale", _tailscale_fallbacks())
    if exe is None:
        return TailscaleState(False, False, None)
    try:
        result = run(
            [str(exe), "status", "--json"],
            capture_output=True,
            text=True,
            timeout=TAILSCALE_TIMEOUT_S,
            check=False,
            creationflags=NO_WINDOW,
        )
        if result.returncode != 0:
            return TailscaleState(True, False, None)
        data = json.loads(result.stdout or "")
        running = data.get("BackendState") == "Running"
        dns = (data.get("Self") or {}).get("DNSName") or ""
        dns_name = dns.rstrip(".").lower() if isinstance(dns, str) and dns else None
        return TailscaleState(True, running, dns_name)
    except Exception:
        return TailscaleState(True, False, None)


def docker_installed() -> bool:
    """Whether Docker's command-line program is on this PC (running or not)."""
    fallbacks = []
    if program_files := os.environ.get("ProgramFiles"):
        fallbacks.append(
            Path(program_files) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
        )
    return find_executable("docker", fallbacks) is not None


def startup_status() -> StartupStatus:
    return startup.cached_status(time.monotonic())


# --- what is set up --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSetup:
    """The model files the checks look for. Paths stay here: no status carries them."""

    chat_mode: Literal["owned", "external", "none"]
    chat_model: str
    chat_paths: tuple[Path, ...] = ()
    images_managed: bool = False
    comfy_paths: tuple[Path, ...] = ()
    image_cards: dict[str, tuple[Path, ...]] = field(default_factory=dict)
    checkable: bool = True  # False without a runtime config: no files to look for


def model_setup(stack: ModelStack, settings: Settings) -> ModelSetup:
    """What the model stack runs, and the files that must exist for it to work. With no
    runtime config there are no paths, so there is nothing to check."""
    chat_model = (
        stack.chat_model_id
        if stack.chat_mode == "owned" and stack.chat_model_id
        else settings.chat_model
    )
    image_ids = sorted(
        c for c in stack.managed if c in stack.cards and stack.cards[c].kind == "image"
    )
    images_managed = stack.comfy is not None and bool(image_ids)
    rt = stack.runtime
    if rt is None:
        return ModelSetup(
            stack.chat_mode, chat_model, images_managed=images_managed, checkable=False
        )

    chat_paths: list[Path] = []
    if stack.chat_mode == "owned":
        if rt.llama_server_exe is not None:
            chat_paths.append(rt.llama_server_exe)
        card = stack.cards.get(chat_model)
        if card is not None:
            chat_paths.extend(card_file_paths(card, rt) or ())
    comfy_paths = tuple(p for p in (rt.comfyui_python, rt.comfyui_main) if p is not None)
    image_cards: dict[str, tuple[Path, ...]] = {}
    if images_managed:
        for card_id in image_ids:
            paths = card_file_paths(stack.cards[card_id], rt)
            if paths is not None:  # no model folder: its files can't be found
                image_cards[card_id] = paths
    return ModelSetup(
        stack.chat_mode,
        chat_model,
        tuple(chat_paths),
        images_managed,
        comfy_paths,
        image_cards,
    )


@dataclass
class Probes:
    """How each check is made. A None probe means the module function of that name, looked
    up when it's called (so a test's monkeypatch of it takes effect)."""

    sandbox: Callable[[], Awaitable[SandboxStatus | None]]
    docker_configured_off: bool
    git_available: Callable[[], bool]
    token_saved: Callable[[], bool]
    chat_reachable: Callable[[], Awaitable[bool]]
    docker_installed: Callable[[], bool] | None = None
    tailscale: Callable[[], TailscaleState] | None = None
    startup: Callable[[], StartupStatus] | None = None
    file_exists: Callable[[Path], bool] = Path.is_file


def sandbox_probe(runner: SandboxRunner | None) -> Callable[[], Awaitable[SandboxStatus | None]]:
    """The analysis check over the sandbox runner: always a fresh look, never its cache."""

    async def probe() -> SandboxStatus | None:
        return None if runner is None else await runner.status(fresh=True)

    return probe


def github_probes(repos: RepoStore | None) -> tuple[Callable[[], bool], Callable[[], bool]]:
    """(git_available, token_saved) from the repo store the GitHub tools use, so the tools
    offered and the GitHub card agree."""
    if repos is None:
        return (lambda: False), (lambda: False)
    return repos.git.available, (lambda: repos.token() is not None)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- the registry ----------------------------------------------------------------------------


class Addons:
    def __init__(
        self,
        settings: Settings,
        setup: ModelSetup,
        probes: Probes,
        gpu: GpuScheduler | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._setup = setup
        self._probes = probes
        self._gpu = gpu
        self._clock = clock
        self._lock = asyncio.Lock()
        self._chat_files_ok: bool | None = None  # None: not checked yet
        self._chat_reachable: bool | None = None
        self._cache: dict[AddonId, AddonStatus] = {}
        self.checked_at: datetime | None = None

    # -- refresh ------------------------------------------------------------------------------

    async def refresh(self, *ids: AddonId) -> list[AddonStatus]:
        """Run the checks for `ids` (all add-ons when none) and return their states."""
        unknown = [i for i in ids if i not in ADDON_IDS]
        if unknown:
            raise ValueError(f"unknown add-on: {', '.join(map(str, unknown))}")
        wanted = list(dict.fromkeys(ids)) if ids else list(ADDON_IDS)
        checks = {
            "chat": self._refresh_chat,
            "analysis": self._refresh_analysis,
            "images": self._refresh_images,
            "github": self._refresh_github,
            "devices": self._refresh_devices,
            "startup": self._refresh_startup,
        }
        async with self._lock:
            await asyncio.gather(*(checks[i]() for i in wanted))
            self.checked_at = self._clock()
        return [self.get(i) for i in wanted]

    async def run_forever(self, interval: float = REFRESH_SECONDS) -> None:
        """Refresh every `interval` seconds until cancelled; a failed refresh is logged."""
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                log.warning("add-on refresh failed: %s", type(exc).__name__)
            await asyncio.sleep(interval)

    async def _probe(self, name: str, call: Callable[[], Awaitable[Any]], fallback: Any) -> Any:
        try:
            return await call()
        except Exception as exc:
            log.warning("add-on check %s failed: %s", name, type(exc).__name__)
            return fallback

    def _threaded(self, fn: Callable[[], Any]) -> Callable[[], Awaitable[Any]]:
        return lambda: asyncio.to_thread(fn)

    def _all_exist(self, paths: Iterable[Path]) -> bool:
        check = self._probes.file_exists
        return all(check(p) for p in paths)

    async def _refresh_chat(self) -> None:
        mode = self._setup.chat_mode
        if mode == "owned":
            paths = self._setup.chat_paths
            self._chat_files_ok = await self._probe(
                "chat files", self._threaded(lambda: self._all_exist(paths)), False
            )
        elif mode == "external":
            self._chat_reachable = bool(
                await self._probe("chat server", self._probes.chat_reachable, False)
            )

    async def _refresh_analysis(self) -> None:
        if self._probes.docker_configured_off:
            self._cache["analysis"] = _status("analysis", "not_set_up")
            return
        state = await self._probe("sandbox", self._probes.sandbox, None)
        if state is None or not state.docker:
            installed = await self._probe("docker", self._threaded(self._docker_installed), False)
            self._cache["analysis"] = _status(
                "analysis", "docker_stopped" if installed else "not_set_up"
            )
        elif not state.image:
            self._cache["analysis"] = _status("analysis", "needs_image")
        else:
            self._cache["analysis"] = _status("analysis", "ready")

    async def _refresh_images(self) -> None:
        setup = self._setup
        if not setup.images_managed:
            self._cache["images"] = _status("images", "not_set_up")
            return
        if not setup.checkable:
            self._cache["images"] = _status("images", "ready")  # nothing to check
            return

        def files_ok() -> bool:
            if not self._all_exist(setup.comfy_paths):
                return False
            return any(self._all_exist(paths) for paths in setup.image_cards.values())

        ok = await self._probe("image files", self._threaded(files_ok), False)
        self._cache["images"] = _status("images", "ready" if ok else "error")

    async def _refresh_github(self) -> None:
        # Warm the (memoised) Git check off the loop, so get() never waits on it.
        await self._probe("git", self._threaded(self._probes.git_available), False)

    async def _refresh_devices(self) -> None:
        origin = urlsplit(self._settings.public_origin)
        host = (origin.hostname or "").lower()
        if host in LOOPBACK_HOSTS:
            self._cache["devices"] = _status("devices", "this_pc_only")
            return
        if origin.scheme == "https" and host.endswith(".ts.net"):
            ts = await self._probe(
                "tailscale", self._threaded(self._tailscale), TailscaleState(True, False, None)
            )
            ok = ts.running and ts.dns_name == host
            self._cache["devices"] = _status("devices", "ready" if ok else "needs_tailscale")
            return
        self._cache["devices"] = _status("devices", "ready")  # a front end we can't check

    async def _refresh_startup(self) -> None:
        state = await self._probe(
            "startup", self._threaded(self._startup), StartupStatus(True, False, None)
        )
        if not state.supported:
            self._cache["startup"] = _status("startup", "unsupported")
        elif not state.enabled:
            self._cache["startup"] = _status("startup", "off")
        elif startup.on_for_another_folder(state, self._settings.data_dir):
            message = MESSAGE_VARIANTS["startup.off_other_folder"]
            self._cache["startup"] = _status("startup", "off", message=message)
        else:
            self._cache["startup"] = _status("startup", "on")

    def _docker_installed(self) -> bool:
        probe = self._probes.docker_installed
        return probe() if probe is not None else docker_installed()

    def _tailscale(self) -> TailscaleState:
        probe = self._probes.tailscale
        return probe() if probe is not None else tailscale_state()

    def _startup(self) -> StartupStatus:
        probe = self._probes.startup
        return probe() if probe is not None else startup_status()

    # -- reading ------------------------------------------------------------------------------

    def get(self, addon_id: AddonId) -> AddonStatus:
        if addon_id == "chat":
            return self._chat()
        if addon_id == "github":
            return self._github()
        cached = self._cache.get(addon_id)
        if cached is not None:
            return cached
        if addon_id not in ADDON_IDS:
            raise KeyError(addon_id)
        return _status(addon_id, _UNCHECKED[addon_id])

    def all(self) -> list[AddonStatus]:
        return [self.get(i) for i in ADDON_IDS]

    def ready(self, *ids: AddonId) -> bool:
        return all(self.get(i).ready for i in ids)

    def tool_ready(self, tool_name: str) -> bool:
        return self.ready(*TOOL_ADDONS.get(tool_name, ()))

    def level_addon(self, level_key: str) -> AddonStatus | None:
        """The add-on a tool level waits on: the first that isn't ready, else the first."""
        ids = LEVEL_ADDONS.get(level_key, ())
        statuses = [self.get(i) for i in ids]
        for status in statuses:
            if not status.ready:
                return status
        return statuses[0] if statuses else None

    def _chat(self) -> AddonStatus:
        setup = self._setup
        model = setup.chat_model
        if setup.chat_mode == "none":
            return _status("chat", "not_set_up")
        if setup.chat_mode == "external":
            if self._chat_reachable:
                return _status("chat", "ready", model=model, loaded=True)
            return _status("chat", "offline")
        if self._chat_files_ok is False:
            return _status("chat", "error")
        gpu = self._gpu
        if gpu is not None and gpu.loading() == model:
            return _status("chat", "starting")
        loaded = gpu is not None and model in gpu.resident()
        message = None if loaded else MESSAGE_VARIANTS["chat.ready_not_loaded"]
        return _status("chat", "ready", message=message, model=model, loaded=loaded)

    def _github(self) -> AddonStatus:
        git = self._live("git", self._probes.git_available)
        token = self._live("token", self._probes.token_saved)
        if not git and not token:
            return _status("github", "not_set_up")
        if not git:
            return _status("github", "needs_git")
        if not token:
            return _status("github", "needs_token")
        return _status("github", "ready")

    def _live(self, name: str, probe: Callable[[], bool]) -> bool:
        try:
            return bool(probe())
        except Exception as exc:
            log.warning("add-on check %s failed: %s", name, type(exc).__name__)
            return False


# Before the first refresh, a cached add-on reports this state.
_UNCHECKED: dict[AddonId, str] = {
    "analysis": "not_set_up",
    "images": "not_set_up",
    "devices": "this_pc_only",
    "startup": "off",
}
