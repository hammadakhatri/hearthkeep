"""The download catalog: every file Hearthkeep may fetch, pinned by URL, size and SHA-256."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import httpx

from hearthkeep.fetching import MODEL_HOST_SUFFIXES, MODEL_HOSTS, allowed_url
from hearthkeep.models.cards import PACKAGE_DIR, ModelCard, load_cards

SHIPPED_CATALOG = PACKAGE_DIR / "catalog.toml"

_ID_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_HF_PIN_RE = re.compile(r"/resolve/[0-9a-f]{40}/")
_KINDS = ("runtime", "chat", "comfyui", "image")
_UNPACK = ("", "zip", "7z")
_VARIANTS = ("", "cuda", "vulkan", "cpu")
_ITEM_KEYS = {
    "id", "kind", "name", "licence", "licence_url", "file", "variant", "card", "needs_nvidia",
    "install_dir", "exe", "python", "main", "note",
}  # fmt: skip
_FILE_KEYS = {"url", "size", "sha256", "unpack", "save_as"}
# A folder that does not exist, so `load_cards` returns the shipped cards only.
_NO_OWNER_FOLDER = Path("no-owner-cards-here")


class CatalogError(Exception):
    """The catalog breaks a rule. The message names the item."""


@dataclass(frozen=True)
class CatalogFile:
    url: str
    size: int
    sha256: str
    unpack: Literal["", "zip", "7z"] = ""
    save_as: str = ""


@dataclass(frozen=True)
class CatalogItem:
    id: str
    kind: Literal["runtime", "chat", "comfyui", "image"]
    name: str
    licence: str
    licence_url: str
    files: tuple[CatalogFile, ...]
    variant: Literal["", "cuda", "vulkan", "cpu"] = ""
    card: str = ""
    needs_nvidia: bool = False
    install_dir: str = ""
    exe: str = ""
    python: str = ""
    main: str = ""
    note: str = ""

    @property
    def size_bytes(self) -> int:
        return sum(f.size for f in self.files)


def _text(raw: dict[str, Any], key: str, where: str, *, required: bool = False) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise CatalogError(f"{where}: {key} must be text")
    if required and not value:
        raise CatalogError(f"{where}: {key} is required")
    return value


def _check_relative(path: str, key: str, where: str) -> None:
    if (
        "\\" in path
        or path.startswith("/")
        or re.match(r"[A-Za-z]:", path)
        or ".." in PurePosixPath(path).parts
    ):
        raise CatalogError(f"{where}: {key} must be a relative path using /, without ..")


def _file(raw: object, where: str) -> CatalogFile:
    if not isinstance(raw, dict):
        raise CatalogError(f"{where}: a file entry must be a table")
    unknown = set(raw) - _FILE_KEYS
    if unknown:
        raise CatalogError(f"{where}: unknown file key(s): {', '.join(sorted(unknown))}")
    url = _text(raw, "url", where, required=True)
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise CatalogError(f"{where}: bad url {url!r}") from exc
    if not allowed_url(parsed, exact=MODEL_HOSTS, suffixes=MODEL_HOST_SUFFIXES):
        raise CatalogError(f"{where}: url {url!r} is not on the allowed hosts over https")
    if parsed.host == "huggingface.co" and not _HF_PIN_RE.search(parsed.path):
        raise CatalogError(f"{where}: a Hugging Face url must pin a commit (/resolve/<40 hex>/)")
    size = raw.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise CatalogError(f"{where}: size must be a positive whole number")
    sha = raw.get("sha256")
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise CatalogError(f"{where}: sha256 must be 64 lowercase hex characters")
    unpack = _text(raw, "unpack", where)
    if unpack not in _UNPACK:
        raise CatalogError(f"{where}: unpack must be zip or 7z")
    return CatalogFile(url, size, sha, unpack, _text(raw, "save_as", where))  # type: ignore[arg-type]


def _item(raw: object, cards: dict[str, ModelCard]) -> CatalogItem:
    if not isinstance(raw, dict):
        raise CatalogError("an item must be a table")
    item_id = raw.get("id")
    if not isinstance(item_id, str) or not _ID_RE.fullmatch(item_id):
        raise CatalogError(f"bad item id {item_id!r}")
    where = f"item {item_id}"
    unknown = set(raw) - _ITEM_KEYS
    if unknown:
        raise CatalogError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")
    kind = raw.get("kind")
    if kind not in _KINDS:
        raise CatalogError(f"{where}: unknown kind {kind!r}")
    variant = _text(raw, "variant", where)
    if variant not in _VARIANTS:
        raise CatalogError(f"{where}: unknown variant {variant!r}")
    licence_url = _text(raw, "licence_url", where, required=True)
    try:
        licence_scheme = httpx.URL(licence_url).scheme
    except httpx.InvalidURL as exc:
        raise CatalogError(f"{where}: licence_url is not a valid url") from exc
    if licence_scheme != "https":
        raise CatalogError(f"{where}: licence_url must be https")
    needs_nvidia = raw.get("needs_nvidia", False)
    if not isinstance(needs_nvidia, bool):
        raise CatalogError(f"{where}: needs_nvidia must be true or false")
    raw_files = raw.get("file")
    if not isinstance(raw_files, list) or not raw_files:
        raise CatalogError(f"{where}: needs at least one file")
    files = tuple(_file(f, where) for f in raw_files)

    item = CatalogItem(
        id=item_id,
        kind=kind,
        name=_text(raw, "name", where, required=True),
        licence=_text(raw, "licence", where, required=True),
        licence_url=licence_url,
        files=files,
        variant=variant,  # type: ignore[arg-type]
        card=_text(raw, "card", where),
        needs_nvidia=needs_nvidia,
        install_dir=_text(raw, "install_dir", where),
        exe=_text(raw, "exe", where),
        python=_text(raw, "python", where),
        main=_text(raw, "main", where),
        note=_text(raw, "note", where),
    )
    if kind == "runtime":
        for key in ("variant", "install_dir", "exe"):
            if not getattr(item, key):
                raise CatalogError(f"{where}: a runtime needs {key}")
    elif kind == "comfyui":
        for key in ("install_dir", "python", "main"):
            if not getattr(item, key):
                raise CatalogError(f"{where}: comfyui needs {key}")
    else:
        card = cards.get(item.card)
        if card is None or card.kind != kind:
            raise CatalogError(f"{where}: card {item.card!r} is not a shipped {kind} card")
        save_as = [f.save_as for f in files]
        if len(set(save_as)) != len(save_as) or set(save_as) != set(card.files):
            raise CatalogError(
                f"{where}: save_as must be exactly the card's files {sorted(card.files)}"
            )
    for key in ("install_dir", "python", "main"):
        value = getattr(item, key)
        if value:
            _check_relative(value, key, where)
    return item


def load_catalog(text: str | None = None) -> dict[str, CatalogItem]:
    """The shipped catalog (or `text`, for tests), in file order. Raises CatalogError."""
    try:
        source = text if text is not None else SHIPPED_CATALOG.read_text(encoding="utf-8")
        raw = tomllib.loads(source)
    except tomllib.TOMLDecodeError as exc:
        raise CatalogError(f"catalog is not valid TOML: {exc}") from exc
    if raw.get("schema") != 1:
        raise CatalogError("unsupported catalog schema")
    unknown = set(raw) - {"schema", "item"}
    if unknown:
        raise CatalogError(f"unknown catalog key(s): {', '.join(sorted(unknown))}")
    items = raw.get("item")
    if not isinstance(items, list) or not items:
        raise CatalogError("the catalog has no items")

    cards, _ = load_cards(_NO_OWNER_FOLDER)
    catalog: dict[str, CatalogItem] = {}
    for entry in items:
        item = _item(entry, cards)
        if item.id in catalog:
            raise CatalogError(f"duplicate item id {item.id!r}")
        catalog[item.id] = item

    chat = [cards[i.card].vram_gb for i in catalog.values() if i.kind == "chat"]
    if not any(v <= 6 for v in chat) or not any(6 < v <= 9 for v in chat):
        raise CatalogError("the catalog needs a chat model for up to 6 GB and one for 6 to 9 GB")
    if "chat-gemma-4-12b" not in catalog or catalog["chat-gemma-4-12b"].card != "gemma-4-12b":
        raise CatalogError("the catalog needs the gemma-4-12b chat model")
    return catalog
