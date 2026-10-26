"""The image tool: the chat model picks an image model, writes the prompt in its style, and the
tool renders through ComfyUI under a GPU lease, stores the images and attaches them to the chat."""

from __future__ import annotations

import re
import secrets
import sqlite3

from hearthkeep.images import ASPECTS, ImageStore, validate_size
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


class GenerateImageTool:
    name = "generate_image"
    level_key = IMAGES_LEVEL_KEY

    def __init__(self, cards: list[ModelCard], default_model: str, gpu, comfy, store: ImageStore):
        image_cards = sorted((c for c in cards if c.kind == "image"), key=lambda c: c.id)
        if not image_cards:
            raise ValueError("the image tool needs at least one image model card")
        self._cards = {c.id: c for c in image_cards}
        if default_model not in self._cards:
            raise ValueError(f"default image model {default_model!r} has no image card")
        self._default = default_model
        self._gpu = gpu
        self._comfy = comfy
        self._store = store
        self.description = _describe(image_cards, self._cards[default_model])
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
            },
            "required": ["prompt"],
            "additionalProperties": False,
        }

    def classify(self, args: dict) -> ActionClass:
        return ActionClass.LOOK

    def summarize(self, args: dict) -> str:
        card = self._cards.get(args.get("model") or self._default)
        model = card.name if card else str(args.get("model"))
        count = args.get("count", MAX_COUNT)
        return f"Create {count} image(s) with {model}: {str(args.get('prompt', ''))[:80]}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
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
        template = load_workflow(card)

        attachments: list[dict] = []
        for i in range(count):
            seed = int(base_seed) + i if base_seed is not None else secrets.randbelow(SEED_RANGE)
            graph = fill_workflow(
                template,
                {
                    "prompt": prompt,
                    "width": width,
                    "height": height,
                    "seed": seed,
                    "steps": card.default_steps,
                    "unet": card.files["unet"],
                    "text_encoder": card.files["text_encoder"],
                    "vae": card.files["vae"],
                    "prefix": prefix,
                },
            )
            try:
                # One lease per image: chat can run between renders, and what was made survives.
                async with self._gpu.lease(card.id):
                    pngs = await self._comfy.render(graph)
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


def _describe(cards: list[ModelCard], default: ModelCard) -> str:
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
    paragraphs.append(STYLE_SENTENCE)
    return "\n\n".join(paragraphs)
