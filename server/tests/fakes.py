"""Test doubles shared across test modules."""

import asyncio
import copy
import secrets

from hearthkeep.auth.webauthn import (
    NewCredential,
    StoredCredential,
    VerificationError,
    b64url_decode,
    b64url_encode,
)
from hearthkeep.models.cards import ModelCard
from hearthkeep.runtime.gpu import GpuScheduler
from hearthkeep.runtime.supervisor import ProcessError


def _as_bytes(value) -> bytes | None:
    """Responses sent over HTTP carry bytes as base64url strings."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        try:
            return b64url_decode(value)
        except VerificationError:
            return None
    return None


class FakeVerifier:
    """Deterministic Verifier: a response is valid iff it echoes the expected challenge.

    Registration response: {"id": str, "public_key": bytes, "challenge": bytes}.
    Authentication response: {"id": str, "challenge": bytes, "sign_count": int}.
    Over HTTP the bytes values are base64url strings.
    """

    def registration_options(
        self, user_id: bytes, exclude_credential_ids: list[str] | None = None
    ) -> dict:
        return {
            "challenge": b64url_encode(secrets.token_bytes(32)),
            "user": b64url_encode(user_id),
            "excludeCredentials": list(exclude_credential_ids or []),
        }

    def verify_registration(self, response: dict, challenge: bytes) -> NewCredential:
        if _as_bytes(response.get("challenge")) != challenge:
            raise VerificationError("challenge mismatch")
        public_key = _as_bytes(response.get("public_key"))
        if public_key is None or not isinstance(response.get("id"), str):
            raise VerificationError("malformed registration response")
        return NewCredential(id=response["id"], public_key=public_key, sign_count=0)

    def authentication_options(self, credential_ids: list[str]) -> dict:
        return {
            "challenge": b64url_encode(secrets.token_bytes(32)),
            "allowCredentials": list(credential_ids),
        }

    def verify_authentication(
        self, response: dict, challenge: bytes, credential: StoredCredential
    ) -> int:
        if _as_bytes(response.get("challenge")) != challenge or response.get("id") != credential.id:
            raise VerificationError("authentication failed")
        count = response["sign_count"]
        # Real passkeys often report 0 forever; only a decrease (or a repeat of a non-zero
        # count) signals a cloned authenticator.
        if count < credential.sign_count or (count == credential.sign_count and count != 0):
            raise VerificationError("sign count did not increase")
        return response["sign_count"]


class FakeChatClient:
    """Scripted chat client: each `stream` call plays the next script entry.

    A script entry is a list of provider events; an Exception instance in the list is raised
    at that point (after the events before it were yielded). Every call records a deep copy
    of the messages and tools it was given.
    """

    def __init__(self, scripts: list[list]):
        self.scripts = list(scripts)
        self.calls: list[dict] = []

    async def stream(self, messages, tools, think):
        self.calls.append(
            {"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools), "think": think}
        )
        if not self.scripts:
            raise AssertionError("FakeChatClient ran out of scripted responses")
        for item in self.scripts.pop(0):
            if isinstance(item, BaseException):
                raise item
            yield item


# --- GPU scheduler ---------------------------------------------------------------------------


def card(model_id: str, vram_gb: float, kind: str = "chat") -> ModelCard:
    provider = "llama_server" if kind == "chat" else "comfyui"
    return ModelCard(id=model_id, name=model_id.title(), kind=kind, provider=provider,
                     vram_gb=vram_gb)  # fmt: skip


CHAT = card("chat", 11.0)
IMAGE = card("image", 10.0, kind="image")


class FakeBackend:
    """Records every call. A load waits on `gates[id]` if one is set; `failures[id]` makes the
    next N loads of that model raise; `ready[id] = False` simulates a crashed server."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.failures: dict[str, int] = {}
        self.unload_failures: dict[str, int] = {}
        self.ready: dict[str, bool] = {}

    def ops(self, *kinds: str) -> list[tuple[str, str]]:
        kinds = kinds or ("load", "unload")
        return [c for c in self.calls if c[0] in kinds]

    async def load(self, c: ModelCard) -> None:
        self.calls.append(("load", c.id))
        gate = self.gates.get(c.id)
        if gate is not None:
            await gate.wait()
        if self.failures.get(c.id, 0):
            self.failures[c.id] -= 1
            raise ProcessError(f"{c.name} couldn't start.")
        self.ready[c.id] = True

    async def unload(self, c: ModelCard) -> None:
        self.calls.append(("unload", c.id))
        if self.unload_failures.get(c.id, 0):
            self.unload_failures[c.id] -= 1
            raise RuntimeError("free failed")
        self.ready[c.id] = False

    async def is_ready(self, c: ModelCard) -> bool:
        self.calls.append(("ready", c.id))
        return self.ready.get(c.id, False)


async def until(condition, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "condition not reached in time"
        await asyncio.sleep(0)


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


class Holder:
    """Holds a lease in a background task until `release()`; `entered` is set once granted."""

    def __init__(self, sched: GpuScheduler, model_id: str, log: list[str] | None = None,
                 name: str | None = None) -> None:  # fmt: skip
        self.entered = asyncio.Event()
        self._release = asyncio.Event()
        self._log, self._name, self._model = log, name or model_id, model_id
        self.task = asyncio.create_task(self._run(sched))

    async def _run(self, sched: GpuScheduler) -> None:
        async with sched.lease(self._model):
            if self._log is not None:
                self._log.append(self._name)
            self.entered.set()
            await self._release.wait()

    async def release(self) -> None:
        self._release.set()
        await self.task


# --- sandbox ---------------------------------------------------------------------------------


class FakeSandbox:
    """Stands in for SandboxRunner: `status()` reports `docker`/`image` as set, and each `run()`
    plays the next queued outcome (a RawRun built from member bytes, or an exception to raise).
    Nothing is ever spawned. With nothing queued, a run is "ok" with only status.json."""

    TAG = "hearthkeep-sandbox:000000000000"

    def __init__(self, root, *, docker: bool = True, image: bool = True) -> None:
        self.root = root
        self.docker, self.image = docker, image
        self.queue: list = []
        self.calls: list[dict] = []

    def queue_run(self, outputs: dict[str, bytes] | None = None, /, **fields) -> None:
        """`outputs` are the members the run returns (status.json is added); `fields` override
        the RawRun's own (`members={}` for a run that returned nothing)."""
        self.queue.append(("run", dict(outputs or {}), fields))

    def queue_error(self, exc: BaseException) -> None:
        self.queue.append(("raise", exc, None))

    async def status(self):
        from hearthkeep.sandbox.runner import SandboxStatus

        return SandboxStatus(docker=self.docker, image=self.image, tag=self.TAG)

    async def run(self, run_id, *, mode, files_dir, manifest, script=None, timeout_s=None):
        from hearthkeep.sandbox.runner import (
            NO_DOCKER_MESSAGE,
            NO_IMAGE_MESSAGE,
            RawRun,
            SandboxUnavailable,
        )

        self.calls.append(
            {"run_id": run_id, "mode": mode, "files_dir": files_dir, "manifest": manifest,
             "script": script, "timeout_s": timeout_s}
        )  # fmt: skip
        if not self.docker:
            raise SandboxUnavailable(NO_DOCKER_MESSAGE)
        if not self.image:
            raise SandboxUnavailable(NO_IMAGE_MESSAGE)
        assert files_dir.is_dir(), "the chat's files folder must exist before a run"
        kind, payload, fields = self.queue.pop(0) if self.queue else ("run", {}, {})
        if kind == "raise":
            raise payload
        job = self.root / "jobs" / run_id
        out = job / "out"
        out.mkdir(parents=True)
        members = {}
        for index, (name, data) in enumerate({"status.json": b"{}", **payload}.items()):
            path = out / f"m{index:03d}"
            path.write_bytes(data)
            members[name] = path
        values = dict(status="ok", exit_code=0, stdout="", stdout_truncated=False,
                      duration_ms=1500, members=members, error=None, job_dir=job)  # fmt: skip
        values.update(fields)
        return RawRun(**values)
