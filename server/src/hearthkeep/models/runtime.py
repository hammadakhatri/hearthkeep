"""The owner's runtime config: where the model servers and model files live."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from hearthkeep.config import ConfigError

_PATH_KEYS = ("llama_server_exe", "comfyui_python", "comfyui_main", "comfyui_model_root")
_PORT_KEYS = ("llama_port", "comfyui_port")
_STR_KEYS = ("default_chat_model", "default_image_model")
_KEYS = {*_PATH_KEYS, *_PORT_KEYS, *_STR_KEYS, "gpu_vram_gb", "model_dirs"}


@dataclass(frozen=True)
class RuntimeConfig:
    llama_server_exe: Path | None = None
    llama_port: int = 8080
    comfyui_python: Path | None = None
    comfyui_main: Path | None = None
    comfyui_port: int = 8188
    comfyui_model_root: Path | None = None
    model_dirs: dict[str, Path] = field(default_factory=dict)
    gpu_vram_gb: float | None = None
    default_chat_model: str = "gemma-4-12b"
    default_image_model: str = "z-image-turbo"


def _path(key: str, value: object) -> Path | None:
    """A configured path; empty or `<placeholder>` values count as unset."""
    if not isinstance(value, str):
        raise ConfigError(f"models.toml key '{key}' must be a string path")
    value = value.strip()
    if not value or value.startswith("<"):
        return None
    return Path(value)


def load_runtime(data_dir: Path) -> RuntimeConfig:
    path = Path(data_dir) / "models.toml"
    if not path.exists():
        return RuntimeConfig()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"models.toml is not valid TOML: {exc}") from exc
    for key in raw:
        if key not in _KEYS:
            raise ConfigError(f"unknown models.toml key: '{key}'")

    values: dict[str, object] = {}
    for key in _PATH_KEYS:
        if key in raw:
            values[key] = _path(key, raw[key])
    for key in _PORT_KEYS:
        if key in raw:
            port = raw[key]
            if not isinstance(port, int) or isinstance(port, bool):
                raise ConfigError(f"models.toml key '{key}' must be an integer")
            if not 1 <= port <= 65535:
                raise ConfigError(f"models.toml key '{key}' must be between 1 and 65535")
            values[key] = port
    for key in _STR_KEYS:
        if key in raw:
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise ConfigError(f"models.toml key '{key}' must be a non-empty string")
            values[key] = raw[key].strip()
    if "gpu_vram_gb" in raw:
        vram = raw["gpu_vram_gb"]
        if isinstance(vram, bool) or not isinstance(vram, int | float) or vram <= 0:
            raise ConfigError("models.toml key 'gpu_vram_gb' must be a positive number")
        values["gpu_vram_gb"] = float(vram)
    if "model_dirs" in raw:
        table = raw["model_dirs"]
        if not isinstance(table, dict):
            raise ConfigError("models.toml 'model_dirs' must be a table")
        dirs: dict[str, Path] = {}
        for card_id, folder in table.items():
            resolved = _path(f"model_dirs.{card_id}", folder)
            if resolved is not None:
                dirs[card_id] = resolved
        values["model_dirs"] = dirs
    return RuntimeConfig(**values)  # type: ignore[arg-type]
