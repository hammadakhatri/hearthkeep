"""Model cards: small TOML files that make a model plug-and-play."""

from __future__ import annotations

import copy
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

PACKAGE_DIR = Path(__file__).parent
SHIPPED_CARDS_DIR = PACKAGE_DIR / "cards"
WORKFLOWS_DIR = PACKAGE_DIR / "workflows"

_ID_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
_KINDS = {"chat": "llama_server", "image": "comfyui"}
_CHAT_FILES = {"model", "mmproj"}
_IMAGE_FILES = {"unet", "text_encoder", "vae"}
_ALL_KEYS = {
    "id", "name", "kind", "provider", "capabilities", "vram_gb", "files", "launch_args",
    "context", "sampling", "workflow", "edit_workflow", "default_steps", "guidance",
    "prefer_when",
}  # fmt: skip
_WILDCARD_HOSTS = {"0.0.0.0", "::", "[::]"}
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_LAUNCH_PLACEHOLDERS = {"model", "mmproj", "port", "api_key_file"}


@dataclass(frozen=True)
class ModelCard:
    id: str
    name: str
    kind: Literal["chat", "image"]
    provider: Literal["llama_server", "comfyui"]
    vram_gb: float
    capabilities: tuple[str, ...] = ()
    files: dict[str, str] = field(default_factory=dict)
    launch_args: tuple[str, ...] = ()
    context: int = 0
    sampling: dict = field(default_factory=dict)
    workflow: str | None = None
    edit_workflow: str | None = None
    default_steps: int | None = None
    guidance: str = ""
    prefer_when: str = ""
    source: Literal["shipped", "owner"] = "shipped"


@dataclass
class CardError:
    id_or_file: str
    message: str


class _Invalid(Exception):
    pass


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: object) -> bool:
    return _is_int(v) or isinstance(v, float)


def _str_list(raw: dict, key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise _Invalid(f"{key} must be an array of strings")
    return tuple(value)


def _check_launch_args(args: tuple[str, ...]) -> None:
    for i, arg in enumerate(args):
        if arg in _WILDCARD_HOSTS or arg.split("=", 1)[-1] in _WILDCARD_HOSTS:
            raise _Invalid(f"launch_args must not bind {arg}; only 127.0.0.1 is allowed")
        if arg == "--host":
            if i + 1 >= len(args) or args[i + 1] != "127.0.0.1":
                raise _Invalid("launch_args --host must be followed by 127.0.0.1")
        elif arg.startswith("--host=") and arg != "--host=127.0.0.1":
            raise _Invalid("launch_args --host must be 127.0.0.1")


def _parse(raw: dict[str, Any], source: Literal["shipped", "owner"]) -> ModelCard:
    unknown = set(raw) - _ALL_KEYS
    if unknown:
        raise _Invalid(f"unknown key(s): {', '.join(sorted(unknown))}")
    for key in ("id", "name", "kind"):
        if key not in raw:
            raise _Invalid(f"missing required field {key}")
        if not isinstance(raw[key], str):
            raise _Invalid(f"{key} must be a string")
    card_id, kind = raw["id"], raw["kind"]
    if not _ID_RE.fullmatch(card_id):
        raise _Invalid(f"id {card_id!r} must match [a-z0-9][a-z0-9.-]{{0,63}}")
    if kind not in _KINDS:
        raise _Invalid(f"unknown kind {kind!r} (expected chat or image)")
    provider = raw.get("provider", _KINDS[kind])
    if provider != _KINDS[kind]:
        raise _Invalid(f"a {kind} card must use provider {_KINDS[kind]}")
    vram = raw.get("vram_gb")
    if not _is_num(vram):
        raise _Invalid("vram_gb is required and must be a number")
    if not 0.5 <= vram <= 80:
        raise _Invalid(f"vram_gb must be between 0.5 and 80, got {vram}")

    files = raw.get("files", {})
    if not isinstance(files, dict) or not all(isinstance(v, str) and v for v in files.values()):
        raise _Invalid("files must be a table of file names")
    for name in files.values():
        if "/" in name or "\\" in name or ":" in name or name in (".", ".."):
            raise _Invalid(f"files entry {name!r} must be a plain file name, not a path")
    sampling = raw.get("sampling", {})
    if not isinstance(sampling, dict):
        raise _Invalid("sampling must be a table")
    for key in ("guidance", "prefer_when"):
        if not isinstance(raw.get(key, ""), str):
            raise _Invalid(f"{key} must be a string")
    capabilities = _str_list(raw, "capabilities")
    launch_args = _str_list(raw, "launch_args")
    _check_launch_args(launch_args)

    context = raw.get("context", 0)
    if not _is_int(context) or context < 0:
        raise _Invalid("context must be a non-negative integer")
    steps = raw.get("default_steps")
    if steps is not None and (not _is_int(steps) or steps < 1):
        raise _Invalid("default_steps must be a positive integer")
    workflows: dict[str, str | None] = {}
    for key in ("workflow", "edit_workflow"):
        value = raw.get(key)
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]+\.json", value):
                raise _Invalid(f"{key} must be a workflow file name ending in .json")
            if not (WORKFLOWS_DIR / value).is_file():
                raise _Invalid(f"{key} {value!r} does not exist in the shipped workflows")
        workflows[key] = value

    for arg in launch_args:
        for name in _PLACEHOLDER_RE.findall(arg):
            if name not in _LAUNCH_PLACEHOLDERS:
                raise _Invalid(f"launch_args uses unknown placeholder {{{name}}}")
            if name in _CHAT_FILES and name not in files:
                raise _Invalid(f"launch_args uses {{{name}}} but files.{name} is not set")

    if kind == "chat":
        if "model" not in files:
            raise _Invalid("a chat card needs files.model")
        if set(files) - _CHAT_FILES:
            raise _Invalid("chat files may only be: model, mmproj")
        if not launch_args:
            raise _Invalid("a chat card needs launch_args")
        if context < 1:
            raise _Invalid("a chat card needs a positive context")
    else:
        if set(files) != _IMAGE_FILES:
            raise _Invalid("an image card needs files: unet, text_encoder, vae")
        if not workflows["workflow"]:
            raise _Invalid("an image card needs a workflow")
        if steps is None:
            raise _Invalid("an image card needs default_steps")
        if launch_args:
            raise _Invalid("only chat cards take launch_args")

    return ModelCard(
        id=card_id,
        name=raw["name"],
        kind=kind,
        provider=provider,
        capabilities=capabilities,
        vram_gb=float(vram),
        files=dict(files),
        launch_args=launch_args,
        context=context,
        sampling=dict(sampling),
        workflow=workflows["workflow"],
        edit_workflow=workflows["edit_workflow"],
        default_steps=steps,
        guidance=raw.get("guidance", ""),
        prefer_when=raw.get("prefer_when", ""),
        source=source,
    )


def _load_dir(
    folder: Path,
    source: Literal["shipped", "owner"],
    cards: dict[str, ModelCard],
    errors: list[CardError],
) -> None:
    if not folder.is_dir():
        return
    seen: set[str] = set()
    for path in sorted(folder.glob("*.toml")):
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
            card = _parse(raw, source)
            if card.id in seen:
                raise _Invalid(f"duplicate card id {card.id!r} in the same folder")
        except OSError as exc:
            errors.append(CardError(path.name, f"cannot read card: {exc}"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            errors.append(CardError(path.name, f"not valid TOML: {exc}"))
        except _Invalid as exc:
            errors.append(CardError(path.name, str(exc)))
        else:
            seen.add(card.id)
            cards[card.id] = card


def load_cards(data_dir: Path) -> tuple[dict[str, ModelCard], list[CardError]]:
    """Shipped cards, then `<data>/models/cards/*.toml` overriding by id."""
    cards: dict[str, ModelCard] = {}
    errors: list[CardError] = []
    _load_dir(SHIPPED_CARDS_DIR, "shipped", cards, errors)
    _load_dir(Path(data_dir) / "models" / "cards", "owner", cards, errors)
    return cards, errors


def load_workflow(card: ModelCard, edit: bool = False) -> dict:
    """A fresh copy of the card's ComfyUI API graph, with `{{placeholders}}` intact."""
    name = card.edit_workflow if edit else card.workflow
    if not name:
        what = "edit workflow" if edit else "workflow"
        raise ValueError(f"model card {card.id} has no {what}")
    graph = json.loads((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))
    return copy.deepcopy(graph)
