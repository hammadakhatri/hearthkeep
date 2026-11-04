"""Where chat comes from once the server (re)starts, by the rule `build_model_stack` uses. Shared
by the models API (what the panel shows) and the installer (whether an owner's external server
must be pinned before a download adds local models)."""

from __future__ import annotations

from pathlib import Path

from hearthkeep.config import ConfigError, Settings, load_settings
from hearthkeep.models.cards import load_cards
from hearthkeep.models.runtime import RuntimeConfig, load_runtime


def read_settings(data: Path, fallback: Settings) -> Settings:
    """config.toml as it is now (it may have changed since the server started)."""
    try:
        return load_settings(Path(data))
    except (ConfigError, OSError):
        return fallback


def _read_runtime(data: Path) -> RuntimeConfig:
    try:
        return load_runtime(data)
    except (ConfigError, OSError):
        return RuntimeConfig()


def chat_state(data: Path, settings: Settings) -> dict:
    """A local llama-server when models.toml sets it up fully and doesn't choose the external
    server; else the external server when config.toml names one; else none."""
    data = Path(data)
    rt = _read_runtime(data)
    card = load_cards(data)[0].get(rt.default_chat_model)
    external_chosen = rt.chat_server == "external" and settings.chat_base_url_set
    if (
        rt.llama_server_exe is not None
        and not external_chosen
        and card is not None
        and card.kind == "chat"
        and card.id in rt.model_dirs
    ):
        return {"source": "local", "model": card.id, "base_url": None}
    if settings.chat_base_url_set:
        return {
            "source": "external",
            "model": settings.chat_model,
            "base_url": settings.chat_base_url,
        }
    return {"source": "none", "model": None, "base_url": None}


def chat_is_external(data: Path, fallback: Settings) -> bool:
    """Whether chat uses an external server now, by `chat_state`'s rule."""
    return chat_state(data, read_settings(data, fallback))["source"] == "external"
