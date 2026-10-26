"""The image tool: the chat model picks an image model, writes the prompt in its style, and the
tool renders through ComfyUI under a GPU lease, stores the images and attaches them to the chat.
Given one of the owner's uploaded photos, it edits that photo with a card that can edit images."""

from __future__ import annotations

import re
import secrets
import sqlite3

from hearthkeep.images import ASPECTS, UPLOAD_EXTENSIONS, ImageStore, validate_size
from hearthkeep.models.cards import ModelCard, load_workflow
from hearthkeep.permissions import ActionClass
from hearthkeep.providers.comfyui import RenderError, fill_workflow
from hearthkeep.runtime.supervisor import ProcessError
from hearthkeep.tools.base import ToolContext, ToolResult

IMAGES_LEVEL_KEY = "images"
PROMPT_MAX_CHARS = 2000
MAX_COUNT = 4
SEED_RANGE = 2**31
SAVE_FAILED = "Hearthkeep couldn't save the image."
STYLE_SENTENCE = "Write the prompt in that model's style. Put any on-image text in double quotes."
_PREFIX_UNSAFE = re.compile(r"[^A-Za-z0-9_/-]")
_UPLOAD_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
EDIT_CAPABILITY = "image-edit"
PHOTO_NOT_FOUND = "I can't find that photo. Ask the owner to attach it again."
NO_EDITOR = "None of the image models here can edit photos."


class GenerateImageTool:
    name = "generate_image"
    level_key = IMAGES_LEVEL_KEY

    def __init__(self, cards: list[ModelCard], default_model: str, gpu, comfy, store: ImageStore):
        image_cards = sorted((c for c in cards if c.kind == "image"), key=lambda c: c.id)
        if not image_cards:
            raise ValueError("the image tool needs at least one image model card")
        self._cards = {c.id: c for c in image_cards}
        self._editors = [c for c in image_cards if EDIT_CAPABILITY in c.capabilities
                         and c.edit_workflow]  # fmt: skip
        if default_model not in self._cards:
            raise ValueError(f"default image model {default_model!r} has no image card")
        self._default = default_model
        self._gpu = gpu
        self._comfy = comfy
        self._store = store
        self.description = _describe(image_cards, self._cards[default_model], self._editors)
        self.parameters = {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": PROMPT_MAX_CHARS,
                    "description": "The image prompt, written in the chosen model's style.",
                },
                "model": {
                    "type": "string",
                    "enum": list(self._cards),
                    "description": "Which image model to use.",
                },
                "aspect": {
                    "type": "string",
                    "enum": list(ASPECTS),
                    "default": "square",
                    "description": "The shape of the image.",
                },
                "width": {"type": "integer", "description": "Width in pixels; overrides aspect."},
                "height": {
                    "type": "integer",
                    "description": "Height in pixels; overrides aspect.",
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_COUNT,
                    "default": MAX_COUNT,
                    "description": "How many images to make.",
                },
                "seed": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": SEED_RANGE - 1,
                    "description": "Fixed seed for reproducible images.",
                },
                "input_image": {
                    "type": "string",
                    "maxLength": 64,
                    "description": "The upload id of the owner's photo to edit or reuse "
                    "(from an 'Attached photo:' line).",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        }

    def classify(self, args: dict) -> ActionClass:
        return ActionClass.LOOK

    def summarize(self, args: dict) -> str:
        editing = args.get("input_image") is not None
        card = self._edit_card(args.get("model"))[0] if editing else None
        card = card or self._cards.get(args.get("model") or self._default)
        model = card.name if card else str(args.get("model"))
        prompt = str(args.get("prompt", ""))[:80]
        if editing:
            return f"Edit the owner's photo with {model}: {prompt}"
        return f"Create {args.get('count', MAX_COUNT)} image(s) with {model}: {prompt}"

    def _edit_card(self, model: str | None) -> tuple[ModelCard | None, str | None]:
        """The card to edit a photo with, else an error for the chat model: the one asked for
        if it can edit, or the first that can when none was named."""
        if not self._editors:
            return None, NO_EDITOR
        if model is None:
            return self._editors[0], None
        card = self._cards[model]
        if card in self._editors:
            return card, None
        names = " or ".join(f"{c.name} ({c.id})" for c in self._editors)
        return None, f"{card.name} can't edit photos. To edit the owner's photo, use {names}."

    def _photo(self, upload_id: object) -> tuple[bytes, str] | None:
        """The photo's bytes and file extension, looked up by id; the path comes from the
        store alone. None when the id is malformed, unknown, or its file is gone."""
        if not isinstance(upload_id, str) or not _UPLOAD_ID.fullmatch(upload_id):
            return None
        found = self._store.get_upload(upload_id)
        path = self._store.upload_path(upload_id) if found else None
        if found is None or path is None:
            return None
        try:
            return path.read_bytes(), UPLOAD_EXTENSIONS[found.mime]
        except (OSError, KeyError):
            return None

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        photo_id = args.get("input_image")
        photo: tuple[bytes, str] | None = None
        if photo_id is not None:
            card, problem = self._edit_card(args.get("model"))
            if card is None:
                return ToolResult(ok=False, content=problem or NO_EDITOR)
            photo = self._photo(photo_id)
            if photo is None:
                return ToolResult(ok=False, content=PHOTO_NOT_FOUND)
        else:
            card = self._cards[args.get("model") or self._default]
        prompt = args["prompt"]
        aspect_w, aspect_h = ASPECTS[args.get("aspect", "square")]
        width, height = args.get("width", aspect_w), args.get("height", aspect_h)
        try:
            validate_size(width, height)
        except ValueError as exc:
            return ToolResult(ok=False, content=f"That image size isn't supported: {exc}")
        count = int(args.get("count", MAX_COUNT))
        base_seed = args.get("seed")
        prefix = "hearthkeep/" + _PREFIX_UNSAFE.sub("_", ctx.turn_id)
        template = load_workflow(card, edit=photo is not None)
        comfy_name: str | None = None  # the photo's name inside ComfyUI, once uploaded

        attachments: list[dict] = []
        for i in range(count):
            seed = int(base_seed) + i if base_seed is not None else secrets.randbelow(SEED_RANGE)
            values = {
                "prompt": prompt,
                "width": width,
                "height": height,
                "seed": seed,
                "steps": card.default_steps,
                "unet": card.files["unet"],
                "text_encoder": card.files["text_encoder"],
                "vae": card.files["vae"],
                "prefix": prefix,
            }
            try:
                # One lease per image: chat can run between renders, and what was made survives.
                async with self._gpu.lease(card.id):
                    if photo is not None and comfy_name is None:
                        # Inside the lease: ComfyUI is only sure to be running while leased.
                        data, ext = photo
                        comfy_name = await self._comfy.upload(data, f"hk-{photo_id}.{ext}")
                    if comfy_name is not None:
                        values["input_image"] = comfy_name
                    pngs = await self._comfy.render(fill_workflow(template, values))
            except (ProcessError, RenderError) as exc:
                return _stopped(exc.user_message, attachments, count)
            try:
                if not pngs:
                    raise ValueError("the render returned no image")
                # Only the first image of a render: a call never attaches more than `count`.
                record = self._store.save(
                    pngs[0],
                    chat_id=ctx.chat_id,
                    turn_id=ctx.turn_id,
                    model_id=card.id,
                    prompt=prompt,
                    width=width,
                    height=height,
                    seed=seed,
                )
            except (OSError, ValueError, sqlite3.Error, OverflowError):
                return _stopped(SAVE_FAILED, attachments, count)
            attachments.append({"kind": "image", "id": record.id})

        return ToolResult(
            ok=True,
            content=(
                f"Created {len(attachments)} image(s) with {card.name} at {width}x{height}. "
                f"Prompt used: {prompt}"
            ),
            data={"attachments": attachments},
        )


def _stopped(message: str, attachments: list[dict], count: int) -> ToolResult:
    """A failed call. Images made before the failure stay attached, and the model is told how
    many there were (never their ids)."""
    content = message
    if attachments:
        content += f" {len(attachments)} of {count} images were made before it stopped."
    return ToolResult(ok=False, content=content, data={"attachments": attachments})


def _describe(cards: list[ModelCard], default: ModelCard, editors: list[ModelCard]) -> str:
    paragraphs = [
        "Create images from a text prompt on the owner's own computer. The images are shown "
        f"in the chat. Pick the image model that fits the request (default: {default.id}):"
    ]
    for card in cards:
        text = f"{card.id} ({card.name})"
        if card.prefer_when:
            text += f": prefer when {card.prefer_when}."
        if card.guidance.strip():
            text += f"\n{card.guidance.strip()}"
        paragraphs.append(text)
    if editors:
        ids = " or ".join(c.id for c in editors)
        paragraphs.append(
            f"To edit or reuse the owner's photo, pass its id as input_image and use {ids}."
        )
    paragraphs.append(STYLE_SENTENCE)
    return "\n\n".join(paragraphs)
