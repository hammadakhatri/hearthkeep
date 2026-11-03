import asyncio
import json
import logging
import subprocess
import threading
from pathlib import Path

import pytest
from copy_rules import forbidden_in

from hearthkeep import addons as addons_mod
from hearthkeep.addons import (
    _COPY,
    ABOUT,
    ADDON_IDS,
    LEVEL_ADDONS,
    MESSAGE_VARIANTS,
    TOOL_ADDONS,
    Addons,
    AddonStatus,
    ModelSetup,
    Probes,
    TailscaleState,
    handle_sandbox_probe,
    model_setup,
    tailscale_state,
)
from hearthkeep.config import Settings
from hearthkeep.models.cards import load_cards
from hearthkeep.models.runtime import RuntimeConfig, card_file_paths
from hearthkeep.procutil import find_executable
from hearthkeep.runtime.stack import ModelStack
from hearthkeep.sandbox.handle import SandboxHandle
from hearthkeep.sandbox.runner import SandboxStatus
from hearthkeep.startup import StartupStatus

CHAT_ID = "gemma-4-12b"
IMAGE_ID = "z-image-turbo"
STATES = {
    "chat": {"not_set_up", "ready", "starting", "offline", "error"},
    "analysis": {"not_set_up", "docker_stopped", "needs_image", "ready"},
    "images": {"not_set_up", "error", "ready"},
    "github": {"not_set_up", "needs_git", "needs_token", "ready"},
    "devices": {"this_pc_only", "needs_tailscale", "ready"},
    "startup": {"off", "on", "unsupported"},
}
ACTIONS = {"install", "start_docker", "build_image", "add_token", "open_setup", "turn_on"}


# --- fakes -----------------------------------------------------------------------------------


class FakeSandboxStatus:
    def __init__(self, *, docker: bool = True, image: bool = True) -> None:
        self.docker, self.image = docker, image
        self.calls = 0

    async def call(self, fresh: bool = False) -> SandboxStatus:
        self.calls += 1
        return SandboxStatus(docker=self.docker, image=self.image, tag="hearthkeep-sandbox:0")


class FakeGpu:
    def __init__(self) -> None:
        self.loading_id: str | None = None
        self.residents: list[str] = []

    def loading(self) -> str | None:
        return self.loading_id

    def resident(self) -> list[str]:
        return list(self.residents)


async def always() -> bool:
    return True


async def never() -> bool:
    return False


EXPLODED: list[str] = []


async def explode() -> bool:
    EXPLODED.append("chat_reachable")
    raise AssertionError("must not be called")


def probes(**over) -> Probes:
    """Probes from fakes, all ready by default."""
    values = dict(
        sandbox=FakeSandboxStatus().call,
        docker_configured_off=False,
        git_available=lambda: True,
        token_saved=lambda: True,
        chat_reachable=always,
        docker_installed=lambda: True,
    )
    values.update(over)
    return Probes(**values)


def settings(tmp_path: Path | None = None, **over) -> Settings:
    return Settings(data_dir=tmp_path or Path("data"), **over)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def owned_setup(tmp_path: Path, *, images: bool = False) -> ModelSetup:
    """An owned chat model (and with `images`, one managed image card) whose files exist."""
    cards, errors = load_cards(tmp_path / "data")
    assert not errors
    gemma = tmp_path / "gemma"
    for name in cards[CHAT_ID].files.values():
        _touch(gemma / name)
    rt = RuntimeConfig(
        llama_server_exe=_touch(tmp_path / "llama" / "llama-server.exe"),
        model_dirs={CHAT_ID: gemma},
    )
    managed = {CHAT_ID}
    if images:
        root = tmp_path / "comfy" / "models"
        rt = RuntimeConfig(
            llama_server_exe=rt.llama_server_exe,
            model_dirs=rt.model_dirs,
            comfyui_python=_touch(tmp_path / "comfy" / "python.exe"),
            comfyui_main=_touch(tmp_path / "comfy" / "main.py"),
            comfyui_model_root=root,
        )
        card = cards[IMAGE_ID]
        for path in card_file_paths(card, rt):
            _touch(path)
        managed.add(IMAGE_ID)
        cards = {CHAT_ID: cards[CHAT_ID], IMAGE_ID: card}
    else:
        cards = {CHAT_ID: cards[CHAT_ID]}
    stack = ModelStack(
        cards=cards,
        managed=frozenset(managed),
        chat_model_id=CHAT_ID,
        comfy=object() if images else None,
        chat_mode="owned",
        runtime=rt,
    )
    return model_setup(stack, settings(tmp_path))


EXTERNAL = ModelSetup("external", CHAT_ID)


# --- copy ------------------------------------------------------------------------------------


def all_states_from_table() -> list[AddonStatus]:
    return [
        AddonStatus(addon_id, state, message, action, short)
        for (addon_id, state), (message, action, short) in _COPY.items()
    ]


async def test_every_state_has_copy_without_commands_or_placeholders():
    statuses = all_states_from_table()
    assert {(s.id, s.state) for s in statuses} == {
        (addon_id, state) for addon_id, states in STATES.items() for state in states
    }
    assert set(ABOUT) == set(ADDON_IDS)
    texts = [text for s in statuses for text in (s.message, s.short or "", *ABOUT[s.id])]
    texts += list(MESSAGE_VARIANTS.values())
    for text in texts:
        assert forbidden_in(text) == [], text
        assert "<" not in text and "--" not in text, text
    assert {s.action for s in statuses} - {None} <= ACTIONS
    for s in statuses:
        assert s.ready == (s.state in ("ready", "on"))


def test_status_json_shape():
    status = AddonStatus("analysis", "needs_image", "m", "build_image", "s")
    assert status.to_json() == {
        "id": "analysis",
        "name": "Analysis",
        "about": ABOUT["analysis"][1],
        "requires": ABOUT["analysis"][2],
        "state": "needs_image",
        "ready": False,
        "message": "m",
        "action": "build_image",
        "short": "s",
        "details": {},
    }
    assert ABOUT["analysis"][0] == "Analysis"


def test_tool_and_level_tables():
    assert ADDON_IDS == ("chat", "analysis", "images", "github", "devices", "startup")
    assert TOOL_ADDONS["repo_test"] == ("github", "analysis")
    assert LEVEL_ADDONS["github_tests"] == ("github", "analysis")


# --- analysis --------------------------------------------------------------------------------


async def test_docker_stopped_after_startup_turns_analysis_off():
    sandbox = FakeSandboxStatus(docker=True, image=True)
    addons = Addons(settings(), EXTERNAL, probes(sandbox=sandbox.call))
    await addons.refresh()
    assert addons.get("analysis").state == "ready" and addons.tool_ready("python")
    sandbox.docker = False
    await addons.refresh("analysis")
    s = addons.get("analysis")
    assert (s.state, s.action) == ("docker_stopped", "start_docker")
    assert not addons.tool_ready("python") and not addons.tool_ready("repo_test")
    assert not addons.tool_ready("read_pages") and not addons.tool_ready("files")
    assert addons.tool_ready("send_to_devices")  # not in TOOL_ADDONS


async def test_docker_not_installed_is_not_set_up():
    async def no_sandbox(fresh=False):
        return None

    addons = Addons(
        settings(), EXTERNAL, probes(sandbox=no_sandbox, docker_installed=lambda: False)
    )
    await addons.refresh("analysis")
    s = addons.get("analysis")
    assert (s.state, s.action, s.short) == ("not_set_up", "install", "Needs Docker")
    installed = Addons(
        settings(), EXTERNAL, probes(sandbox=no_sandbox, docker_installed=lambda: True)
    )
    await installed.refresh("analysis")
    assert installed.get("analysis").state == "docker_stopped"


async def test_docker_configured_off_is_not_set_up():
    sandbox = FakeSandboxStatus()
    addons = Addons(settings(), EXTERNAL, probes(sandbox=sandbox.call, docker_configured_off=True))
    await addons.refresh()
    s = addons.get("analysis")
    assert s.state == "not_set_up"
    # Not "isn't installed": Docker may well be installed, it's turned off in config.toml.
    assert s.message == MESSAGE_VARIANTS["analysis.docker_off"]
    assert s.message == "Docker is turned off in Hearthkeep's settings."
    assert (s.action, s.short) == ("open_setup", "Docker turned off")
    assert sandbox.calls == 0


class FakeRunner:
    async def status(self, fresh: bool = False) -> SandboxStatus:
        return SandboxStatus(docker=True, image=False, tag="hearthkeep-sandbox:0")


async def test_check_again_sees_docker_that_started_after_hearthkeep():
    """Docker missing at start-up, started a few seconds later: the background refresh may
    still land inside the handle's 30 s throttle, but "Check again" (fresh) looks now."""
    up, now = [False], [0.0]

    async def factory():
        return FakeRunner()

    handle = SandboxHandle(factory=factory, docker_check=lambda: up[0], clock=lambda: now[0])
    addons = Addons(
        settings(),
        EXTERNAL,
        probes(sandbox=handle_sandbox_probe(handle), docker_installed=lambda: True),
    )
    await addons.refresh("analysis")
    assert addons.get("analysis").state == "docker_stopped"
    up[0], now[0] = True, 10.0  # Docker starts; still inside the 30 s window
    await addons.refresh("analysis")
    assert addons.get("analysis").state == "docker_stopped"  # throttled, as before
    await addons.refresh("analysis", fresh=True)
    assert addons.get("analysis").state == "needs_image"


async def test_handle_get_fresh_skips_the_throttle():
    checks, now = [], [0.0]

    def check():
        checks.append(now[0])
        return False

    async def factory():
        return FakeRunner()

    handle = SandboxHandle(factory=factory, docker_check=check, clock=lambda: now[0])
    assert await handle.get() is None
    now[0] = 5.0
    assert await handle.get() is None and checks == [0.0]
    assert await handle.get(fresh=True) is None and checks == [0.0, 5.0]


async def test_missing_image_is_needs_image():
    sandbox = FakeSandboxStatus(docker=True, image=False)
    addons = Addons(settings(), EXTERNAL, probes(sandbox=sandbox.call))
    await addons.refresh("analysis")
    s = addons.get("analysis")
    assert (s.state, s.action, s.short) == (
        "needs_image",
        "build_image",
        "Needs the analysis sandbox",
    )


# --- chat and images -------------------------------------------------------------------------


async def test_deleted_card_files_report_error_without_paths(tmp_path):
    setup = owned_setup(tmp_path, images=True)
    addons = Addons(settings(tmp_path), setup, probes(), gpu=FakeGpu())
    await addons.refresh()
    assert addons.get("chat").state == "ready"
    assert addons.get("images").state == "ready"

    (tmp_path / "gemma" / "gemma-4-12b-it-Q6_K.gguf").unlink()
    await addons.refresh("chat")
    chat = addons.get("chat")
    assert (chat.state, chat.action) == ("error", "open_setup")
    assert chat.message == "Hearthkeep can't find the chat model's files on this PC."
    assert str(tmp_path) not in json.dumps(chat.to_json())

    (tmp_path / "comfy" / "models" / "vae" / "ae.safetensors").unlink()
    await addons.refresh("images")
    images = addons.get("images")
    assert images.state == "error"
    assert str(tmp_path) not in json.dumps([s.to_json() for s in addons.all()])


async def test_missing_comfyui_is_images_error(tmp_path):
    setup = owned_setup(tmp_path, images=True)
    (tmp_path / "comfy" / "main.py").unlink()
    addons = Addons(settings(tmp_path), setup, probes(), gpu=FakeGpu())
    await addons.refresh("images")
    assert addons.get("images").state == "error"


async def test_images_not_managed_is_not_set_up(tmp_path):
    addons = Addons(settings(tmp_path), owned_setup(tmp_path), probes(), gpu=FakeGpu())
    await addons.refresh()
    s = addons.get("images")
    assert (s.state, s.action, s.short) == ("not_set_up", "install", "Needs Images")


async def test_external_down_is_offline_never_configured_is_not_set_up():
    down = Addons(settings(), ModelSetup("external", CHAT_ID), probes(chat_reachable=never))
    await down.refresh()
    s = down.get("chat")
    assert (s.state, s.action, s.short) == ("offline", "open_setup", "Chat server offline")
    EXPLODED.clear()
    none = Addons(settings(), ModelSetup("none", CHAT_ID), probes(chat_reachable=explode))
    await none.refresh()
    assert none.get("chat").state == "not_set_up"
    assert EXPLODED == []  # chat_reachable was never called


async def test_external_up_is_ready_and_loaded():
    addons = Addons(settings(), EXTERNAL, probes())
    await addons.refresh("chat")
    s = addons.get("chat")
    assert (s.state, s.ready, s.details) == ("ready", True, {"model": CHAT_ID, "loaded": True})


async def test_owned_chat_starting_then_ready_not_loaded(tmp_path):
    gpu = FakeGpu()
    addons = Addons(settings(tmp_path), owned_setup(tmp_path), probes(), gpu=gpu)
    await addons.refresh()
    gpu.loading_id = CHAT_ID
    assert addons.get("chat").state == "starting"
    assert not addons.ready("chat")
    gpu.loading_id = None
    s = addons.get("chat")
    assert (s.state, s.details) == ("ready", {"model": CHAT_ID, "loaded": False})
    assert s.message == MESSAGE_VARIANTS["chat.ready_not_loaded"]
    gpu.residents = [CHAT_ID]
    s = addons.get("chat")
    assert (s.state, s.details, s.message) == (
        "ready",
        {"model": CHAT_ID, "loaded": True},
        "Ready.",
    )


def test_model_setup_without_runtime_has_nothing_to_check(tmp_path):
    setup = model_setup(ModelStack(chat_mode="owned"), settings(tmp_path))
    assert (setup.chat_paths, setup.comfy_paths, setup.image_cards) == ((), (), {})
    external = model_setup(ModelStack(), settings(tmp_path, chat_model="other"))
    assert (external.chat_mode, external.chat_model) == ("external", "other")


def test_model_setup_lists_the_owned_files(tmp_path):
    setup = owned_setup(tmp_path, images=True)
    assert setup.chat_mode == "owned" and setup.chat_model == CHAT_ID
    assert setup.chat_paths[0] == tmp_path / "llama" / "llama-server.exe"
    assert tmp_path / "gemma" / "gemma-4-12b-it-Q6_K.gguf" in setup.chat_paths
    assert setup.images_managed
    assert setup.comfy_paths == (tmp_path / "comfy" / "python.exe", tmp_path / "comfy" / "main.py")
    assert set(setup.image_cards) == {IMAGE_ID}
    assert tmp_path / "comfy" / "models" / "vae" / "ae.safetensors" in setup.image_cards[IMAGE_ID]


async def test_images_without_runtime_have_nothing_to_check(tmp_path):
    cards, _ = load_cards(tmp_path)
    stack = ModelStack(cards={IMAGE_ID: cards[IMAGE_ID]}, managed=frozenset({IMAGE_ID}),
                       comfy=object())  # fmt: skip
    setup = model_setup(stack, settings(tmp_path))
    assert setup.images_managed and not setup.checkable
    addons = Addons(settings(tmp_path), setup, probes(file_exists=lambda p: False))
    await addons.refresh("images")
    assert addons.get("images").state == "ready" and addons.tool_ready("generate_image")


# --- github, devices, startup ----------------------------------------------------------------


async def test_github_states_follow_git_and_token_live():
    state = {"git": True, "token": True}
    addons = Addons(
        settings(),
        EXTERNAL,
        probes(git_available=lambda: state["git"], token_saved=lambda: state["token"]),
    )
    await addons.refresh()
    assert addons.get("github").state == "ready" and addons.tool_ready("repo_read")
    state["token"] = False  # no refresh: get() changes at once
    s = addons.get("github")
    assert (s.state, s.action, s.short) == ("needs_token", "add_token", "Needs a GitHub token")
    assert not addons.tool_ready("github")
    state["git"] = False
    assert addons.get("github").state == "not_set_up"
    state["token"] = True
    s = addons.get("github")
    assert (s.state, s.action, s.short) == ("needs_git", "install", "Needs Git")


async def test_devices_states(tmp_path):
    calls: list[str] = []

    def tailscale(dns: str | None, running: bool = True):
        def probe() -> TailscaleState:
            calls.append("tailscale")
            return TailscaleState(True, running, dns)

        return probe

    local = Addons(settings(tmp_path), EXTERNAL, probes(tailscale=tailscale("pc.tail1.ts.net")))
    await local.refresh()
    s = local.get("devices")
    assert (s.state, s.action) == ("this_pc_only", "open_setup")
    assert calls == []  # Tailscale is never run for a loopback origin

    origin = {"public_origin": "https://pc.tail1.ts.net", "rp_id": "pc.tail1.ts.net"}
    ready = Addons(
        settings(tmp_path, **origin), EXTERNAL, probes(tailscale=tailscale("pc.tail1.ts.net"))
    )
    await ready.refresh("devices")
    assert ready.get("devices").state == "ready" and calls == ["tailscale"]
    assert "tail1" not in json.dumps(ready.get("devices").to_json())

    wrong = Addons(
        settings(tmp_path, **origin), EXTERNAL, probes(tailscale=tailscale("other.tail1.ts.net"))
    )
    await wrong.refresh("devices")
    assert wrong.get("devices").state == "needs_tailscale"
    stopped = Addons(
        settings(tmp_path, **origin),
        EXTERNAL,
        probes(tailscale=tailscale("pc.tail1.ts.net", running=False)),
    )
    await stopped.refresh("devices")
    assert stopped.get("devices").state == "needs_tailscale"

    other = {"public_origin": "https://home.example.org", "rp_id": "home.example.org"}
    calls.clear()
    elsewhere = Addons(settings(tmp_path, **other), EXTERNAL, probes(tailscale=tailscale(None)))
    await elsewhere.refresh("devices")
    assert elsewhere.get("devices").state == "ready" and calls == []


@pytest.mark.parametrize(
    ("status", "state", "message"),
    [
        (StartupStatus(False, False, None), "unsupported", None),
        (StartupStatus(True, True, "SAME"), "on", None),
        (StartupStatus(True, False, None), "off", None),
        (StartupStatus(True, True, "elsewhere"), "off", "startup.off_other_folder"),
    ],
)
async def test_startup_states(tmp_path, status, state, message):
    if status.data_dir == "SAME":
        status = StartupStatus(True, True, str(tmp_path.resolve()))
    elif status.data_dir == "elsewhere":
        status = StartupStatus(True, True, str(tmp_path / "other"))
    addons = Addons(settings(tmp_path), EXTERNAL, probes(startup=lambda: status))
    await addons.refresh("startup")
    s = addons.get("startup")
    assert s.state == state
    assert s.ready == (state == "on")
    if message is not None:
        assert s.message == MESSAGE_VARIANTS[message]
    if state == "off":
        assert s.action == "turn_on"


async def test_level_addon_picks_first_not_ready():
    sandbox = FakeSandboxStatus(docker=False)
    addons = Addons(settings(), EXTERNAL, probes(sandbox=sandbox.call))
    await addons.refresh()
    assert addons.level_addon("github_tests").id == "analysis"
    assert addons.level_addon("python").id == "analysis"
    assert addons.level_addon("github").id == "github"  # ready: the first of them
    assert addons.level_addon("send_to_devices") is None
    assert addons.ready("github") and not addons.ready("github", "analysis")


# --- how probes run --------------------------------------------------------------------------


async def test_checks_run_off_the_event_loop(tmp_path):
    seen: dict[str, list[int]] = {n: [] for n in ("docker", "files", "git", "tail", "start")}

    def blocking(name: str, value=True):
        seen[name].append(threading.get_ident())
        return value

    async def no_sandbox(fresh=False):
        return None

    addons = Addons(
        settings(tmp_path, public_origin="https://pc.tail1.ts.net", rp_id="pc.tail1.ts.net"),
        owned_setup(tmp_path, images=True),
        probes(
            sandbox=no_sandbox,
            docker_installed=lambda: blocking("docker"),
            file_exists=lambda p: blocking("files"),
            git_available=lambda: blocking("git"),
            tailscale=lambda: blocking("tail", TailscaleState(True, True, "pc.tail1.ts.net")),
            startup=lambda: blocking("start", StartupStatus(True, False, None)),
        ),
        gpu=FakeGpu(),
    )
    await addons.refresh()
    loop_thread = threading.get_ident()
    for name in ("docker", "files", "tail", "start"):
        assert seen[name] and loop_thread not in seen[name], name
    # get() reads Git live (it is memoised); refresh warms it in a thread first.
    assert seen["git"] and seen["git"][0] != loop_thread


class CountingProbes:
    """Counts refresh-time probe calls only (git_available and token_saved run live in get)."""

    def __init__(self) -> None:
        self.count = 0

    def bump(self, value):
        self.count += 1
        return value

    def probes(self) -> Probes:
        async def sandbox(fresh=False):
            return self.bump(SandboxStatus(docker=True, image=True, tag="t"))

        async def reachable():
            return self.bump(True)

        return probes(
            sandbox=sandbox,
            chat_reachable=reachable,
            docker_installed=lambda: self.bump(True),
            tailscale=lambda: self.bump(TailscaleState(False, False, None)),
            startup=lambda: self.bump(StartupStatus(True, False, None)),
            file_exists=lambda p: self.bump(True),
        )


async def test_get_reads_the_cache_and_only_refresh_probes(tmp_path):
    counting = CountingProbes()
    addons = Addons(settings(tmp_path), owned_setup(tmp_path, images=True), counting.probes(),
                    gpu=FakeGpu())  # fmt: skip
    assert addons.checked_at is None
    await addons.refresh()
    after_refresh = counting.count
    assert after_refresh > 0 and addons.checked_at is not None
    for _ in range(5):
        addons.all()
        addons.get("chat")
        addons.tool_ready("python")
        addons.level_addon("github_tests")
    assert counting.count == after_refresh
    await addons.refresh("startup")
    assert counting.count == after_refresh + 1


async def test_concurrent_refreshes_share_one_lock():
    active = {"now": 0, "max": 0}

    async def slow_sandbox(fresh=False):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.01)
        active["now"] -= 1
        return SandboxStatus(docker=True, image=True, tag="t")

    addons = Addons(settings(), EXTERNAL, probes(sandbox=slow_sandbox))
    await asyncio.gather(addons.refresh("analysis"), addons.refresh("analysis"), addons.refresh())
    assert active["max"] == 1


async def test_probe_that_raises_gives_not_ready_and_logs_type_only(caplog):
    secret = "token-abc123-secret-detail"

    async def bad_sandbox(fresh=False):
        raise RuntimeError(secret)

    def bad_docker() -> bool:
        raise OSError(secret)

    async def bad_reachable() -> bool:
        raise ValueError(secret)

    def bad_startup():
        raise PermissionError(secret)

    addons = Addons(
        settings(),
        EXTERNAL,
        probes(
            sandbox=bad_sandbox,
            docker_installed=bad_docker,
            chat_reachable=bad_reachable,
            startup=bad_startup,
        ),
    )
    with caplog.at_level(logging.WARNING, logger="hearthkeep.addons"):
        await addons.refresh()
    assert addons.get("analysis").state == "not_set_up"
    assert addons.get("chat").state == "offline"
    assert addons.get("startup").state == "off"
    assert secret not in caplog.text
    for name in ("RuntimeError", "OSError", "ValueError", "PermissionError"):
        assert name in caplog.text


async def test_run_forever_refreshes_and_stops_on_cancel():
    sandbox = FakeSandboxStatus()
    addons = Addons(settings(), EXTERNAL, probes(sandbox=sandbox.call))
    task = asyncio.create_task(addons.run_forever(interval=0.001))
    for _ in range(200):
        if sandbox.calls >= 3:
            break
        await asyncio.sleep(0.005)
    assert sandbox.calls >= 3
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_forever_survives_a_failing_refresh(monkeypatch):
    addons = Addons(settings(), EXTERNAL, probes())
    calls = {"n": 0}

    async def failing(*ids):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(addons, "refresh", failing)
    task = asyncio.create_task(addons.run_forever(interval=0.001))
    for _ in range(200):
        if calls["n"] >= 2:
            break
        await asyncio.sleep(0.005)
    assert calls["n"] >= 2 and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_unknown_addon_id_is_refused():
    addons = Addons(settings(), EXTERNAL, probes())
    with pytest.raises(ValueError):
        await addons.refresh("wallet")  # type: ignore[arg-type]


async def test_hermetic_fixture_patches_the_module_probes():
    assert addons_mod.docker_installed() is False
    assert addons_mod.tailscale_state() == TailscaleState(False, False, None)
    assert addons_mod.startup_status() == StartupStatus(False, False, None)


# --- tailscale, card paths, executables ------------------------------------------------------


def _exe(monkeypatch, path: Path | None) -> None:
    monkeypatch.setattr(addons_mod, "find_executable", lambda name, fallbacks=(): path)


def test_tailscale_state_parses_status_json(monkeypatch, tmp_path):
    exe = tmp_path / "tailscale.exe"
    _exe(monkeypatch, exe)
    seen = {}

    def run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        out = json.dumps({"BackendState": "Running", "Self": {"DNSName": "PC.tail1.ts.net."}})
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    assert tailscale_state(run=run) == TailscaleState(True, True, "pc.tail1.ts.net")
    assert seen["argv"] == [str(exe), "status", "--json"]
    assert seen["kwargs"]["timeout"] == 5
    assert seen["kwargs"]["creationflags"] == addons_mod.NO_WINDOW

    def stopped(argv, **kwargs):
        out = json.dumps({"BackendState": "Stopped", "Self": {"DNSName": ""}})
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    assert tailscale_state(run=stopped) == TailscaleState(True, False, None)


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="[]", stderr=""),
        subprocess.CompletedProcess([], 1, stdout="", stderr="failed"),
        subprocess.TimeoutExpired("tailscale", 5),
        FileNotFoundError("gone"),
    ],
)
def test_tailscale_state_without_binary_or_bad_json(monkeypatch, tmp_path, outcome):
    _exe(monkeypatch, None)
    assert tailscale_state(run=lambda *a, **k: pytest.fail("must not run")) == TailscaleState(
        False, False, None
    )
    _exe(monkeypatch, tmp_path / "tailscale.exe")

    def run(argv, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    assert tailscale_state(run=run) == TailscaleState(True, False, None)


def test_card_file_paths_for_chat_and_image(tmp_path):
    cards, _ = load_cards(tmp_path)
    chat, image = cards[CHAT_ID], cards[IMAGE_ID]
    rt = RuntimeConfig(model_dirs={CHAT_ID: tmp_path / "g"}, comfyui_model_root=tmp_path / "root")
    assert card_file_paths(chat, rt) == tuple(tmp_path / "g" / n for n in chat.files.values())
    paths = card_file_paths(image, rt)
    assert tmp_path / "root" / "vae" / "ae.safetensors" in paths
    assert tmp_path / "root" / "diffusion_models" / "z_image_turbo_bf16.safetensors" in paths
    assert tmp_path / "root" / "text_encoders" / "qwen_3_4b.safetensors" in paths
    assert card_file_paths(chat, RuntimeConfig()) is None
    assert card_file_paths(image, RuntimeConfig()) is None


def test_find_executable_ignores_current_folder(tmp_path, monkeypatch):
    from hearthkeep import procutil

    cwd = tmp_path / "cwd"
    _touch(cwd / "tool.exe")
    monkeypatch.chdir(cwd)
    fallback = _touch(tmp_path / "pf" / "tool.exe")
    for found in (None, "tool.exe", r".\tool.exe", str(cwd / "tool.exe")):
        monkeypatch.setattr(procutil.shutil, "which", lambda name, found=found: found)
        assert find_executable("tool") is None
        assert find_executable("tool", [tmp_path / "missing.exe", fallback]) == fallback
        assert find_executable("tool", [Path("relative.exe")]) is None
    elsewhere = _touch(tmp_path / "bin" / "tool.exe")
    monkeypatch.setattr(procutil.shutil, "which", lambda name: str(elsewhere))
    assert find_executable("tool", [fallback]) == elsewhere


# --- "Check again" and the background loop ----------------------------------------------------


async def test_fresh_refresh_bypasses_the_startup_cache(monkeypatch):
    calls = []

    def fake_status(*, run=None):
        calls.append(1)
        return StartupStatus(True, False, None)

    monkeypatch.setattr(addons_mod.startup, "status", fake_status)
    monkeypatch.setattr(
        addons_mod,
        "startup_status",
        lambda fresh=False: addons_mod.startup.cached_status(100.0, fresh=fresh),
    )
    addons_mod.startup.clear_cache()
    addons = Addons(settings(), EXTERNAL, probes(startup=None))
    await addons.refresh("startup")
    await addons.refresh("startup")
    assert len(calls) == 1  # the 30 s cache
    await addons.refresh("startup", fresh=True)
    assert len(calls) == 2
    addons_mod.startup.clear_cache()


async def test_run_forever_sleeps_before_its_first_pass():
    sandbox = FakeSandboxStatus()
    addons = Addons(settings(), EXTERNAL, probes(sandbox=sandbox.call))
    task = asyncio.create_task(addons.run_forever(interval=60))
    await asyncio.sleep(0.05)
    assert sandbox.calls == 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
