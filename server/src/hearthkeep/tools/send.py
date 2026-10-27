"""The `send_to_devices` tool: the model sends a chart, table or file of this chat, or its own
summary, to the owner's devices ("Sent to you"). Sending to the owner's own devices is Look.
A summary is number-checked with this turn's runs. Devices with push on get a payload-free
push in the background; the tool never waits for it."""

from __future__ import annotations

from hearthkeep.auth.credentials import CredentialStore
from hearthkeep.permissions import ActionClass
from hearthkeep.sent import KINDS, MAX_SUMMARY_CHARS, SendRefused, SentStore, question_before
from hearthkeep.tools.base import ToolContext, ToolResult
from hearthkeep.tools.text import one_line

SEND_LEVEL_KEY = "send"
DEVICE_MAX_CHARS = 64
ALL = "all"
DESCRIPTION = (
    "Send something to the owner's devices, where it appears under Sent to you. kind=chart, "
    "table or file sends that output of this chat (or, for file, a file attached to this "
    "chat) by its id; kind=summary sends text you write (at most 4000 characters). device is "
    "a device name, or all (the default)."
)
BAD_KIND = "kind must be chart, table, file or summary."
BAD_DEVICE = "device must be a device name or all."
NO_TEXT = "A summary needs its text."
TOO_LONG = f"A summary can be at most {MAX_SUMMARY_CHARS} characters."


class SendToDevicesTool:
    name = "send_to_devices"
    level_key = SEND_LEVEL_KEY
    description = DESCRIPTION
    parameters = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "id": {
                "type": "string",
                "pattern": "^[A-Za-z0-9_-]{1,32}$",
                "description": "The chart, table or file id (not used for a summary).",
            },
            "text": {
                "type": "string",
                "maxLength": MAX_SUMMARY_CHARS,
                "description": "The summary's text (kind=summary only).",
            },
            "device": {
                "type": "string",
                "maxLength": DEVICE_MAX_CHARS,
                "default": ALL,
                "description": "A device name, or all.",
            },
        },
        "required": ["kind"],
        "additionalProperties": False,
    }

    def __init__(self, sent: SentStore, credentials: CredentialStore) -> None:
        self._sent = sent
        self._credentials = credentials

    def classify(self, args: dict) -> ActionClass:
        return ActionClass.LOOK

    def summarize(self, args: dict) -> str:
        kind = args.get("kind")
        device = args.get("device", ALL)
        kind_text = kind if kind in KINDS else "something"
        device_text = one_line(device)[:DEVICE_MAX_CHARS].strip() if isinstance(device, str) else ""
        return f"Send {kind_text} to {device_text or ALL}"

    def _devices(self, device: str) -> list[str] | str:
        """`all`, or the ids of the devices with this name (any case); an error text listing
        the device names when none has it."""
        wanted = " ".join(device.split()).casefold()
        if wanted == ALL:
            return ALL
        devices = self._credentials.unrevoked()
        matched = [c.id for c in devices if c.device_name.casefold() == wanted]
        if matched:
            return matched
        names = ", ".join(one_line(c.device_name) for c in devices)
        shown = one_line(device)[:DEVICE_MAX_CHARS].strip()
        return f'No device is called "{shown}". The devices are: {names}.'

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        kind = args.get("kind")
        if kind not in KINDS:
            return ToolResult(ok=False, content=BAD_KIND)
        device = args.get("device", ALL)
        if not isinstance(device, str) or len(device) > DEVICE_MAX_CHARS:
            return ToolResult(ok=False, content=BAD_DEVICE)
        devices = self._devices(device)
        if isinstance(devices, str) and devices != ALL:
            return ToolResult(ok=False, content=devices)
        ref = args.get("id")
        text = args.get("text")
        try:
            item = self._sent.send(
                chat_id=ctx.chat_id,
                kind=kind,
                ref_id=None if kind == "summary" else ref if isinstance(ref, str) else None,
                text=text if kind == "summary" and isinstance(text, str) else None,
                devices=devices,
                turn_id=ctx.turn_id,
                question=question_before(ctx.conn, ctx.chat_id, None) if kind == "summary" else "",
                actor="agent",
            )
        except SendRefused as refused:
            return ToolResult(ok=False, content=_refusal(refused.reason, kind))
        self._sent.notify_later(item)
        return ToolResult(ok=True, content=f"Sent to {len(self._sent.targets(item.id))} device(s).")


def _refusal(reason: str, kind: str) -> str:
    if reason == "no_text":
        return NO_TEXT
    if reason == "too_long":
        return TOO_LONG
    if reason == "unknown_device":
        return BAD_DEVICE
    return f"There is no {kind} with that id in this chat."
