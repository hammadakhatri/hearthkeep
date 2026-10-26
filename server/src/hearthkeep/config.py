"""Settings and data folder handling."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlparse

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
EXAMPLE_CONFIG = Path(__file__).with_name("config.example.toml")
DATA_SUBDIRS = (
    "secrets",
    "workspace/output",
    "workspace/output/images",
    "workspace/uploads",
    "workspace/chats",
    "workspace/repos",
    "logs",
    "private",
)


class ConfigError(Exception):
    """Raised when config.toml is invalid or unsafe."""


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    public_origin: str = "http://localhost:8765"
    rp_id: str = "localhost"
    chat_base_url: str = "http://127.0.0.1:8080/v1"
    chat_model: str = "gemma-4-12b"
    chat_api_key_secret: str = "chat_api_key"
    session_idle_days: int = 14
    approval_ttl_minutes: int = 15
    max_agent_steps: int = 8
    docker_available: bool | None = None


_TYPES = {f.name: f.type for f in fields(Settings) if f.name != "data_dir"}


def _check_type(key: str, value: object) -> None:
    expected = _TYPES[key]
    if key == "docker_available":
        ok = isinstance(value, bool)
    elif expected == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    else:
        ok = isinstance(value, str)
    if not ok:
        raise ConfigError(f"config key '{key}' has the wrong type: {type(value).__name__}")


_RANGES = (
    ("port", 1, 65535),
    ("session_idle_days", 1, None),
    ("approval_ttl_minutes", 1, None),
    ("max_agent_steps", 1, 50),
)


def load_settings(data_dir: Path) -> Settings:
    data_dir = Path(data_dir)
    path = data_dir / "config.toml"
    raw: dict[str, object] = {}
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"config.toml is not valid TOML: {exc}") from exc
    for key, value in raw.items():
        if key not in _TYPES:
            raise ConfigError(f"unknown config key: '{key}'")
        _check_type(key, value)
    settings = Settings(data_dir=data_dir, **raw)
    _validate(settings)
    return settings


def _validate(s: Settings) -> None:
    if s.host not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"host '{s.host}' is not a loopback address. Hearthkeep only listens on "
            "127.0.0.1; to reach it remotely use Tailscale (`tailscale serve`)."
        )
    origin = urlparse(s.public_origin)
    hostname = origin.hostname
    if origin.scheme not in ("http", "https") or not hostname:
        raise ConfigError(f"public_origin '{s.public_origin}' is not a valid http(s) URL")
    if origin.scheme == "http" and hostname not in LOOPBACK_HOSTS:
        raise ConfigError("public_origin must use https unless it is localhost")
    chat = urlparse(s.chat_base_url)
    if chat.scheme not in ("http", "https") or chat.hostname not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"chat_base_url '{s.chat_base_url}' must point at a loopback host "
            "(127.0.0.1, ::1, localhost): Hearthkeep only talks to local models."
        )
    for key, lo, hi in _RANGES:
        value = getattr(s, key)
        if value < lo or (hi is not None and value > hi):
            bound = f"between {lo} and {hi}" if hi is not None else f"at least {lo}"
            raise ConfigError(f"config key '{key}' must be {bound}, got {value}")
    if s.rp_id != hostname:
        raise ConfigError(f"rp_id '{s.rp_id}' must equal the public_origin host '{hostname}'")


def detect_docker() -> bool:
    """True if `docker info` succeeds within 5 seconds; never raises."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0


def init_data_dir(data_dir: Path) -> None:
    data_dir = Path(data_dir)
    for sub in DATA_SUBDIRS:
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    target = data_dir / "config.toml"
    if not target.exists():
        shutil.copyfile(EXAMPLE_CONFIG, target)
