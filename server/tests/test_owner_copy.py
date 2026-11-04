"""Messages the owner or the model reads name a screen, never a command or a path placeholder."""

import asyncio
import logging

import pytest
from copy_rules import forbidden_in

from hearthkeep import addons
from hearthkeep.agent import loop
from hearthkeep.github import deps
from hearthkeep.models.cards import load_cards
from hearthkeep.models.runtime import RuntimeConfig
from hearthkeep.providers import chat, scheduled_chat
from hearthkeep.runtime.stack import ModelStack
from hearthkeep.runtime.supervisor import (
    ManagedProcess,
    ProcessError,
    _require,
    build_llama_process,
)
from hearthkeep.sandbox import runner
from hearthkeep.tools import python

SCREEN = "Settings › Add-ons"


@pytest.mark.parametrize(
    "text",
    [
        python.NO_DOCKER,
        python.NO_IMAGE,
        runner.NO_DOCKER_MESSAGE,
        runner.NO_IMAGE_MESSAGE,
        runner.DATA_PATH_MESSAGE,
        deps.REPO_NO_DOCKER,
        deps.REPO_NO_IMAGE,
        scheduled_chat._NOT_STARTED,
        chat._REJECTED,
        chat._UNREACHABLE,
        chat.NO_CHAT_MODEL,
        loop.NOT_READY,
        loop.TURNED_OFF,
    ],
)
def test_owner_messages_have_no_commands_or_placeholders(text):
    assert forbidden_in(text) == []


@pytest.mark.parametrize(
    "text",
    [
        python.NO_DOCKER,
        python.NO_IMAGE,
        runner.NO_IMAGE_MESSAGE,
        deps.REPO_NO_IMAGE,
        scheduled_chat._NOT_STARTED,
        chat._REJECTED,
    ],
)
def test_messages_that_send_the_owner_somewhere_name_the_screen(text):
    assert SCREEN in text


def test_exact_copy():
    assert python.NO_DOCKER == (
        "Python analysis needs Docker Desktop running on this PC. "
        "Open Settings › Add-ons to set it up."
    )
    assert python.NO_IMAGE == (
        "Python analysis needs the analysis sandbox, which isn't built yet. "
        "Open Settings › Add-ons to set it up."
    )
    assert chat._REJECTED == (
        "The chat server rejected Hearthkeep's key. "
        "Open Settings › Add-ons to see the chat model's state."
    )


@pytest.mark.parametrize(
    ("key", "screen"),
    [
        (("chat", "not_set_up"), "Settings › Chat model"),
        (("images", "not_set_up"), "Settings › Image models"),
    ],
)
def test_addons_not_set_up_names_the_model_screens(key, screen):
    message, action, short = addons._COPY[key]
    assert screen in message and forbidden_in(message) == []
    assert "docs" not in message.lower()
    assert action == ("open_setup" if key[0] == "chat" else "install")
    if key[0] == "chat":
        assert short == "Needs a chat model"


def test_unavailable_message_still_maps_by_identity():
    assert (
        python.unavailable_message(runner.SandboxUnavailable(runner.NO_IMAGE_MESSAGE))
        is python.NO_IMAGE
    )
    assert (
        python.unavailable_message(runner.SandboxUnavailable(runner.NO_DOCKER_MESSAGE))
        is python.NO_DOCKER
    )


def process(tmp_path, **kw) -> ManagedProcess:
    return ManagedProcess(
        "llama-server",
        ["x"],
        "http://127.0.0.1:1/health",
        tmp_path / "logs" / "llama-server.log",
        data_dir=tmp_path,
        **kw,
    )


def test_process_start_failure_names_a_screen(tmp_path):
    err = process(tmp_path)._start_failed()
    assert SCREEN in err.user_message and forbidden_in(err.user_message) == []
    assert err.user_message == "llama-server couldn't start. Open Settings › Add-ons to check it."
    assert str(tmp_path) not in str(err)
    assert "logs/llama-server.log" in str(err)  # the log location stays in the detail


def test_supervisor_config_messages_have_no_placeholder(tmp_path):
    with pytest.raises(ProcessError) as exc:
        _require(None, "llama_server_exe")
    assert (
        exc.value.user_message
        == "Set llama_server_exe in models.toml in Hearthkeep's data folder first."
    )
    cards, _ = load_cards(tmp_path)
    rt = RuntimeConfig(llama_server_exe=tmp_path / "llama-server.exe", model_dirs={})
    with pytest.raises(ProcessError) as exc2:
        build_llama_process(cards["gemma-4-12b"], rt, tmp_path, tmp_path / "k", api_key=lambda: "k")
    for message in (exc.value.user_message, exc2.value.user_message):
        assert forbidden_in(message) == [] and "models.toml in Hearthkeep's data folder" in message
    assert exc2.value.user_message.startswith('Set model_dirs."gemma-4-12b" in models.toml')


def test_gpu_fallback_warning_has_no_placeholder(caplog):
    stack = ModelStack(detect_vram=lambda: None)
    with caplog.at_level(logging.WARNING):
        asyncio.run(stack.build_gpu())
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "gpu_vram_gb" in text and forbidden_in(text) == []
    assert "models.toml in Hearthkeep's data folder" in text
