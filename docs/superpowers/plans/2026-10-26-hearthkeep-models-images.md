# Hearthkeep Phase 2: Models & Images Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make models plug-and-play through model cards, let Hearthkeep start, stop and swap the chat and image models on one GPU by itself, and let the owner create images (Z-Image-Turbo, FLUX.2 klein) from chat and from the Images view, with the assistant writing each model's prompt in that model's preferred style.

**Architecture:** Model cards (TOML, shipped in the package plus the owner's own in the data folder) describe each model; a per-owner `models.toml` in the data folder holds machine-specific paths. A process supervisor runs llama-server and ComfyUI as child processes bound to 127.0.0.1. A GPU scheduler hands out leases: the chat client takes a lease for every stream, the image tool takes one per render, and the scheduler unloads or stops whatever doesn't fit. Images are stored in the workspace, recorded in SQLite and served only to a signed-in session.

**Tech Stack:** Phase 1 stack (Python 3.12+/uv, FastAPI, httpx, pytest; React 18 + TS, Vitest). No new runtime dependencies (stdlib `tomllib`, `subprocess`, `asyncio`).

**Spec:** `docs/design.md` sections 7 (Models), 8.3 "Image generation", 10 (Interface). Phase 1 plan: `docs/superpowers/plans/2026-10-26-hearthkeep-foundation.md` (its Global Constraints still apply). Owner's private design and Phase 1 ledger: `<data>/private/`.

## Global Constraints

- Everything from the Phase 1 plan's Global Constraints still binds (127.0.0.1 only, nothing personal in the repo, step-up for Outward, no model tool that changes settings, secrets never in prompts/logs/audit, always dark, no attribution lines in commits).
- Every child process (llama-server, ComfyUI) is started bound to `127.0.0.1`; Hearthkeep refuses a card or runtime config that would bind elsewhere.
- Every HTTP client Hearthkeep creates uses `trust_env=False`.
- Generating an image into the workspace is a **Look** action (`level_key` `"images"`, default Allowed). Saving or sending an image anywhere else is out of scope for Phase 2.
- Machine-specific values (executable paths, model file paths) live only in `<data>/models.toml`; the repo ships `models.example.toml` with placeholders.
- Shipped cards: `gemma-4-12b` (chat), `z-image-turbo` (image), `flux2-klein-4b` (image). Shipped workflow graphs are the two tested ComfyUI API graphs (Z-Image-Turbo: 8 steps, `res_multistep`, `simple`, cfg 1, shift 3, text encoder `qwen_3_4b.safetensors` type `lumina2`, VAE `ae.safetensors`; FLUX.2 klein distilled: 4 steps, `euler`, `Flux2Scheduler`, CFGGuider cfg 1 with `ConditioningZeroOut` negative, text encoder `qwen_3_4b.safetensors` type `flux2`, VAE `flux2-vae.safetensors`).
- Image sizes are multiples of 16, each side 512–2048. Named aspects: `square` 1024×1024, `landscape` 1536×864, `portrait` 864×1536, `banner` 1536×640.
- At most 4 images per request; default 4.
- Not in this phase (stated so nobody builds it early): the owner's brand kit, saving images to Google Drive or anywhere outside the workspace, deleting images, exact-text overlay on images, adding cards from the UI. Chat-level permission grants stay keyed by `level_key` (Phase 1 behaviour); image generation is Look, so it never creates grants.

## Review Focus

1. **An image is requested while the chat model is streaming a reply in another chat** → the render waits until that stream ends; the reply is never cut off; both finish. (Task 4: `test_image_lease_waits_for_active_chat_stream`.)
2. **ComfyUI is down, crashes, or a render fails** → the tool returns a plain error ("Image generation isn't available right now: …"), no stray files, and the chat model is brought back so the conversation continues. (Task 6: `test_render_failure_restores_chat_model`.)
3. **The chat model fails to come back after a render** → the next chat stream reports "The chat model couldn't be started. Check <data>/logs/llama-server.log." and a later message retries the start. (Task 4: `test_chat_restart_failure_is_reported_and_retried`.)
4. **Someone asks for an image file by a made-up id, another path, or `..`** → 404, and nothing outside `<data>/workspace/output/images` is ever read. (Task 7: `test_image_file_rejects_unknown_and_traversal`.)
5. **A model card is broken (missing field, unknown kind, wrong types) or its files are missing** → that card is listed with a plain error in `/api/models`, every other card still works, and the server starts. (Task 2: `test_bad_card_reported_not_fatal`; Task 7: `test_models_endpoint_lists_card_errors`.)

---

## File structure

```
server/src/hearthkeep/
  models/
    cards.py            # ModelCard, load_cards(), validation
    runtime.py          # RuntimeConfig from <data>/models.toml
    cards/gemma-4-12b.toml  z-image-turbo.toml  flux2-klein-4b.toml
    workflows/z-image-turbo.json  flux2-klein-4b.json  flux2-klein-4b-edit.json
  runtime/
    supervisor.py       # ManagedProcess
    gpu.py              # GpuScheduler, leases
  providers/
    scheduled_chat.py   # ScheduledChatClient (lease around each stream)
    comfyui.py          # ComfyUIClient
  images.py             # ImageStore (DB + files), sizes
  tools/images.py       # GenerateImageTool
  api/routes_models.py  api/routes_images.py
models.example.toml     # repo root, placeholders only
web/src/views/Images.tsx  web/src/components/ImageGrid.tsx  web/src/components/ModelList.tsx
```

---

### Task 1: Image records and the images folder

**Files:**
- Modify: `server/src/hearthkeep/db.py` (migration 2; `utcnow`)
- Create: `server/src/hearthkeep/images.py`
- Test: `server/tests/test_images_store.py`, `server/tests/test_db.py`

**Interfaces:**
- Produces: migration 2 adds `images(id TEXT PK, chat_id TEXT, turn_id TEXT, model_id TEXT, prompt TEXT, width INT, height INT, seed INT, file_name TEXT, created_at TEXT)` and `uploads(id TEXT PK, file_name TEXT, mime TEXT, width INT, height INT, created_at TEXT)`; `user_version` = 2.
- Produces: `db.utcnow()` now always uses `isoformat(timespec="microseconds")` (fixes the Phase 1 deferred minor; string comparisons of timestamps stay ordered).
- Produces: `ASPECTS: dict[str, tuple[int, int]] = {"square": (1024, 1024), "landscape": (1536, 864), "portrait": (864, 1536), "banner": (1536, 640)}`; `validate_size(width: int, height: int) -> None` (raises `ValueError`); `@dataclass ImageRecord(id, chat_id, turn_id, model_id, prompt, width, height, seed, created_at)`; `class ImageStore: __init__(conn, data_dir: Path); save(png: bytes, *, chat_id, turn_id, model_id, prompt, width, height, seed) -> ImageRecord; list(limit: int = 60, before: str | None = None) -> list[ImageRecord]; get(id) -> ImageRecord | None; path(id) -> Path | None`. Files live in `<data>/workspace/output/images/<id>.png`; ids are `token_urlsafe(12)`; `path()` returns None unless the resolved file is inside that folder and exists. `save` writes the file atomically and inserts the row in one `db.transaction`; if the insert fails the file is removed.

- [ ] **Step 1: Write the failing tests**: `test_migration_2_adds_images_and_uploads`, `test_utcnow_has_microseconds` (`len(db.utcnow().split(".")[1]) >= 6`), `test_save_writes_file_and_row`, `test_list_newest_first_and_paginates`, `test_path_rejects_unknown_id_and_traversal` (`path("../x")`, `path("nope")` → None), `test_save_rolls_back_file_when_insert_fails`, `test_validate_size` (1024×1024 ok; 1000×1024, 256×256, 4096×1024 raise).
- [ ] **Step 2: Run** `cd server && uv run pytest tests/test_images_store.py tests/test_db.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** migration 2, `utcnow`, and `images.py`.
- [ ] **Step 4: Run the tests, then the full suite.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): image records and images folder"`

### Task 2: Model cards and the owner's runtime config

**Files:**
- Create: `server/src/hearthkeep/models/__init__.py`, `models/cards.py`, `models/runtime.py`, `models/cards/*.toml`, `models/workflows/z-image-turbo.json`, `models/workflows/flux2-klein-4b.json`, `models.example.toml`
- Modify: `server/pyproject.toml` (package data for `cards/*.toml`, `workflows/*.json`)
- Test: `server/tests/test_model_cards.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) ModelCard: id: str; name: str; kind: Literal["chat","image"]; provider: Literal["llama_server","comfyui"]; capabilities: tuple[str, ...]; vram_gb: float; files: dict[str, str]` (logical file keys, e.g. `{"model": "...gguf", "mmproj": "...gguf"}` for chat; `{"unet": ..., "text_encoder": ..., "vae": ...}` for images — the values are **file names**, resolved against runtime folders); `launch_args: tuple[str, ...]` (llama-server only; placeholders `{model}`, `{mmproj}`, `{port}`, `{api_key_file}`); `context: int`; `sampling: dict`; `workflow: str | None` (image only, file name in `workflows/`); `edit_workflow: str | None`; `default_steps: int | None`; `guidance: str` (prompting guidance shown to the chat model); `prefer_when: str` (one-line selection hint); `source: Literal["shipped","owner"]`.
- Produces: `@dataclass CardError(id_or_file: str, message: str)`; `load_cards(data_dir: Path) -> tuple[dict[str, ModelCard], list[CardError]]` (shipped cards, then `<data>/models/cards/*.toml` overriding by `id`); `load_workflow(card: ModelCard, edit: bool = False) -> dict` (a fresh deep copy).
- Produces: `@dataclass(frozen=True) RuntimeConfig: llama_server_exe: Path | None; llama_port: int = 8080; comfyui_python: Path | None; comfyui_main: Path | None; comfyui_port: int = 8188; model_dirs: dict[str, Path]` (per card id, the folder holding that card's files) `; gpu_vram_gb: float | None` (override; else detected); `default_chat_model: str = "gemma-4-12b"; default_image_model: str = "z-image-turbo"`; `load_runtime(data_dir: Path) -> RuntimeConfig` reading `<data>/models.toml` (missing file → all None/defaults; unknown keys → `ConfigError`; ports 1–65535).
- `models.example.toml` (repo root): placeholders only, e.g. `llama_server_exe = "<path to llama-server.exe>"`.
- Shipped card values: `gemma-4-12b`: kind chat, provider llama_server, vram_gb 11.0, context 32768, capabilities `["vision","tools","thinking"]`, sampling `{temperature=1.0, top_k=64, top_p=0.95, min_p=0.0}`, files `model="gemma-4-12b-it-Q6_K.gguf"`, `mmproj="mmproj-F16.gguf"`, launch_args `["--model","{model}","--mmproj","{mmproj}","--alias","gemma-4-12b","--host","127.0.0.1","--port","{port}","--api-key-file","{api_key_file}","-c","32768","-ngl","999","--jinja"]`. `z-image-turbo`: kind image, provider comfyui, vram_gb 10.5, capabilities `["text-in-image","photoreal"]`, default_steps 8, files `unet="z_image_turbo_bf16.safetensors"`, `text_encoder="qwen_3_4b.safetensors"`, `vae="ae.safetensors"`, guidance: plain descriptive English, describe subject, setting, light, lens/style; put exact on-image text in double quotes and say where it goes and its style; keep text short; leave space where text goes; prefer_when: "the image contains words, or a photoreal scene". `flux2-klein-4b`: kind image, provider comfyui, vram_gb 10.0, capabilities `["photoreal","image-edit"]`, default_steps 4, files `unet="flux-2-klein-4b.safetensors"`, `text_encoder="qwen_3_4b.safetensors"`, `vae="flux2-vae.safetensors"`, edit_workflow `flux2-klein-4b-edit.json` (added in Task 10), guidance: rich photographic description, materials and lighting; avoid asking for text (it misspells); prefer_when: "no text in the image, or editing the owner's own photo".
- Validation: `id` matches `[a-z0-9][a-z0-9.-]{0,63}`; required fields per kind; `launch_args` must not contain `0.0.0.0` or a `--host` other than `127.0.0.1`; `vram_gb` 0.5–80; `workflow` file must exist in `workflows/`. A failing card becomes a `CardError` and is skipped.
- Workflow JSON placeholders (string values replaced at render time): `"{{prompt}}"`, `"{{width}}"`, `"{{height}}"`, `"{{seed}}"`, `"{{steps}}"`, `"{{unet}}"`, `"{{text_encoder}}"`, `"{{vae}}"`, `"{{prefix}}"`, and for edit `"{{input_image}}"`. Numbers are substituted as ints.

- [ ] **Step 1: Write the failing tests**: `test_shipped_cards_load` (three ids, kinds, providers, vram values above), `test_owner_card_overrides_shipped` (same id in `<data>/models/cards/`), `test_bad_card_reported_not_fatal` (missing `kind`, wrong type for `vram_gb`, unknown kind → each a `CardError`, others still load), `test_card_rejects_non_loopback_host` (`--host 0.0.0.0` → CardError), `test_load_workflow_returns_copy` (mutating the result doesn't affect the next call), `test_workflows_have_placeholders` (each shipped workflow contains `{{prompt}}`, `{{width}}`, `{{height}}`, `{{seed}}`, `{{steps}}`, `{{prefix}}`), `test_runtime_defaults_without_file`, `test_runtime_rejects_unknown_key`, `test_example_runtime_has_placeholders_only` (no drive letters).
- [ ] **Step 2: Run** `uv run pytest tests/test_model_cards.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the loaders, the three cards and the two workflow graphs (convert the tested graphs above into JSON with placeholders).
- [ ] **Step 4: Run the tests, then the full suite.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): model cards and owner runtime config"`

### Task 3: Process supervisor

**Files:**
- Create: `server/src/hearthkeep/runtime/__init__.py`, `runtime/supervisor.py`
- Test: `server/tests/test_supervisor.py` (uses a tiny fake server script in `server/tests/fixtures/fake_service.py` that serves `/health` on a given port and can be told to fail)

**Interfaces:**
- Produces: `class ProcessError(Exception)` with `user_message`; `class ManagedProcess: __init__(name: str, argv: list[str], health_url: str, log_path: Path, *, ready_timeout: float = 120.0, cwd: Path | None = None, env: dict | None = None); async start() -> None` (no-op if healthy; spawns, then polls `health_url` with `trust_env=False` until 200 or timeout → stops the child and raises `ProcessError`); `async stop(timeout: float = 10.0) -> None` (terminate, then kill); `async is_healthy() -> bool`; `running: bool` property. Output goes to `log_path` (appended). On Windows spawn with `CREATE_NEW_PROCESS_GROUP` and no console window (`CREATE_NO_WINDOW`).
- Produces: `build_llama_process(card: ModelCard, rt: RuntimeConfig, data_dir: Path, api_key_file: Path) -> ManagedProcess` (fills `launch_args` placeholders with absolute paths from `rt.model_dirs[card.id]`; health `http://127.0.0.1:{port}/health`) and `build_comfyui_process(rt: RuntimeConfig, data_dir: Path) -> ManagedProcess` (argv `[comfyui_python, "-s", comfyui_main, "--listen", "127.0.0.1", "--port", str(port), "--disable-auto-launch", "--extra-model-paths-config", <data>/runtime/comfyui-paths.yaml]`; writes that YAML pointing ComfyUI at the image cards' model folders; health `http://127.0.0.1:{port}/system_stats`). Both raise `ProcessError` with a plain message naming the missing setting when `models.toml` lacks a path.
- Logs: `<data>/logs/llama-server.log`, `<data>/logs/comfyui.log`.
- The chat API key file: written by Hearthkeep from the SecretStore value to `<data>/runtime/llama-api-key` (created with owner-only permissions where the OS supports it), re-written on every start; never logged.

- [ ] **Step 1: Write the failing tests**: `test_start_waits_for_health`, `test_start_noop_when_already_healthy`, `test_start_timeout_raises_and_kills_child`, `test_stop_kills_unresponsive_child`, `test_output_goes_to_log_file`, `test_llama_argv_filled_and_loopback` (contains `--host 127.0.0.1`, absolute model paths, no unreplaced `{`), `test_missing_runtime_path_raises_plain_error`, `test_comfyui_paths_yaml_written` (points at the configured folders, no other paths).
- [ ] **Step 2: Run** `uv run pytest tests/test_supervisor.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `supervisor.py` with `asyncio.create_subprocess_exec`.
- [ ] **Step 4: Run the tests, then the full suite.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): process supervisor for local model servers"`

### Task 4: GPU scheduler and the scheduled chat client

**Files:**
- Create: `server/src/hearthkeep/runtime/gpu.py`, `server/src/hearthkeep/providers/scheduled_chat.py`
- Test: `server/tests/test_gpu.py`, `server/tests/test_scheduled_chat.py`

**Interfaces:**
- Consumes: `ModelCard` (Task 2), `ManagedProcess` (Task 3), `OpenAIChatClient` (Phase 1).
- Produces: `class Backend(Protocol): async def load(card: ModelCard) -> None; async def unload(card: ModelCard) -> None; async def is_ready(card: ModelCard) -> bool` with `LlamaBackend(process_for: Callable[[ModelCard], ManagedProcess])` (load = start, unload = stop, is_ready = health check) and `ComfyBackend(process: ManagedProcess, free: Callable[[], Awaitable[None]])` (load = ensure ComfyUI running; unload = POST `/free {"unload_models": true, "free_memory": true}`; ComfyUI itself keeps running; is_ready = ComfyUI healthy). Before granting a lease on a model marked resident, the scheduler checks `is_ready`; if the process died it reloads it (crash recovery).
- Produces: `detect_vram_gb() -> float | None` (`nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits`, 5 s timeout, None on failure); `RESERVED_GB = 1.5`.
- Produces: `class GpuScheduler: __init__(cards: dict[str, ModelCard], backends: dict[str, Backend], total_vram_gb: float); lease(model_id: str) -> AsyncContextManager[None]; resident() -> list[str]; status() -> dict` (`{"total_gb", "used_gb", "resident": [...], "waiting": n}`).
  Rules: a lease on a resident model starts at once and may share with other leases on the same model. A lease on a non-resident model waits until no leases are active on any model it must evict, then unloads just enough residents (least recently used first) so `sum(vram) <= total - RESERVED_GB`, then loads it. Requests are served first-come-first-served (an image lease queued behind chat streams doesn't get starved by new chat streams arriving later). A card bigger than the budget raises `ProcessError("This model needs more GPU memory than this PC has.")`. A failed load leaves nothing marked resident and raises.
- Produces: `class ScheduledChatClient: __init__(inner: OpenAIChatClient, gpu: GpuScheduler, model_id: str)` with the same `stream(messages, tools, think)` as `OpenAIChatClient`: holds `gpu.lease(model_id)` for the whole stream; a `ProcessError` while loading becomes `ProviderError(user_message="The chat model couldn't be started. Check <data>/logs/llama-server.log.")`.

- [ ] **Step 1: Write the failing tests** with fake backends recording calls: `test_resident_model_lease_is_immediate_and_shared`, `test_image_lease_waits_for_active_chat_stream` (chat lease held → image lease pending; release → chat unloaded, image loaded), `test_fifo_no_starvation` (image waiting, a new chat lease arrives → image goes first), `test_lru_eviction_only_as_needed` (budget fits two small cards), `test_oversized_card_raises`, `test_failed_load_marks_nothing_resident`, `test_crashed_resident_model_is_reloaded` (is_ready False → load called again before the lease starts), `test_status_shape`, and in `test_scheduled_chat.py`: `test_stream_holds_lease_for_whole_stream`, `test_chat_restart_failure_is_reported_and_retried` (first load raises → ProviderError with the log hint; second stream succeeds after the backend recovers).
- [ ] **Step 2: Run** `uv run pytest tests/test_gpu.py tests/test_scheduled_chat.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `gpu.py` (one `asyncio.Lock` plus a FIFO of waiters) and `scheduled_chat.py`.
- [ ] **Step 4: Run the tests, then the full suite.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): GPU scheduler with leases and scheduled chat client"`

### Task 5: ComfyUI client

**Files:**
- Create: `server/src/hearthkeep/providers/comfyui.py`
- Test: `server/tests/test_comfyui.py` (httpx `MockTransport`)

**Interfaces:**
- Produces: `class RenderError(Exception)` with `user_message`; `class ComfyUIClient: __init__(base_url: str, transport=None, poll_interval: float = 0.5, timeout: float = 600.0); async render(graph: dict) -> list[bytes]` (POST `/prompt {"prompt": graph}` → `prompt_id`; poll `/history/{id}` until `status.completed` or `status_str == "error"`; then GET `/view?filename=…&subfolder=…&type=output` for every image of every `SaveImage` output; returns PNG bytes); `async upload(png: bytes, name: str) -> str` (POST `/upload/image` multipart, returns the stored name); `async free() -> None`; `fill_workflow(graph: dict, values: dict) -> dict` (replaces `"{{key}}"` strings; ints for numeric keys; raises `KeyError` on an unreplaced placeholder). Base URL must be loopback (checked). `trust_env=False`.
- User messages: connection failure → "Image generation isn't available right now: ComfyUI isn't reachable."; ComfyUI error status → "Image generation failed: <node type> reported an error." (no raw tracebacks); timeout → "Image generation took too long and was stopped."

- [ ] **Step 1: Write the failing tests**: `test_render_returns_png_bytes`, `test_render_error_status_maps_to_render_error`, `test_unreachable_maps_to_render_error`, `test_timeout_maps_to_render_error`, `test_fill_workflow_replaces_and_types`, `test_fill_workflow_rejects_unreplaced`, `test_non_loopback_base_url_rejected`, `test_upload_returns_name`.
- [ ] **Step 2: Run** `uv run pytest tests/test_comfyui.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `comfyui.py`.
- [ ] **Step 4: Run the tests, then the full suite.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): ComfyUI client"`

### Task 6: The image tool and chat attachments

**Files:**
- Create: `server/src/hearthkeep/tools/images.py`
- Modify: `server/src/hearthkeep/agent/loop.py` (attachments event), `server/src/hearthkeep/cli.py` (`build_registry` wiring)
- Test: `server/tests/test_image_tool.py`, `server/tests/test_agent_loop.py`

**Interfaces:**
- Consumes: `ImageStore`, `ASPECTS`, `validate_size` (Task 1); cards + `load_workflow` + `RuntimeConfig` (Task 2); `GpuScheduler` (Task 4); `ComfyUIClient`, `fill_workflow`, `RenderError` (Task 5).
- Produces: `class GenerateImageTool` — `name = "generate_image"`, `level_key = "images"`, `classify → ActionClass.LOOK`. Built with `GenerateImageTool(cards: list[ModelCard], default_model: str, gpu, comfy, store)`. Its `description` is assembled from the available image cards: one paragraph per card with `id`, `name`, `prefer_when` and `guidance`, followed by "Write the prompt in that model's style. Put any on-image text in double quotes." `parameters` schema: `prompt` (string, 1–2000 chars, required), `model` (enum of image card ids, optional), `aspect` (enum of ASPECTS keys, default `"square"`), `width`/`height` (optional ints, override aspect), `count` (1–4, default 4), `seed` (optional int ≥ 0). `summarize(args)` → `"Create {count} image(s) with {model name}: {prompt[:80]}"`.
- `run`: picks the card (`args.model` or `default_model`), validates size, then for each image: random seed (or `seed + i`), `fill_workflow`, `async with gpu.lease(card.id): pngs = await comfy.render(graph)`, `store.save(...)`. Returns `ToolResult(ok=True, content="Created N image(s) with <name> at WxH. Prompt used: <prompt>", data={"attachments": [{"kind": "image", "id": rec.id} ...]})`. A `RenderError` → `ToolResult(ok=False, content=user_message)` with already-saved images still attached.
- Loop change: after a tool runs, if `result.data` has `"attachments"` (a list of `{kind, id}` with `kind == "image"` and an id of `[A-Za-z0-9_-]{1,32}`), emit `attachments{step_id, items}` and persist it like other events. Invalid items are dropped. The tool message sent back to the model is only `result.content` (never ids it could misuse).
- `cli.build_registry(...)` registers `GenerateImageTool` when at least one image card loaded and ComfyUI is configured; otherwise it isn't registered (the Tools screen still lists the `images` level).

- [ ] **Step 1: Write the failing tests** with fake gpu/comfy/store: `test_description_lists_each_image_card_guidance`, `test_defaults_square_four_images`, `test_aspect_and_explicit_size`, `test_invalid_size_is_error_result_not_raise`, `test_render_failure_restores_chat_model` (a real `GpuScheduler` with fake backends: chat resident → render raises → next chat lease reloads chat; tool result ok False with the user message), `test_partial_success_keeps_saved_images`, `test_each_render_holds_an_image_lease`; in `test_agent_loop.py`: `test_attachments_event_emitted_and_persisted`, `test_invalid_attachment_items_dropped`, `test_model_never_sees_attachment_ids`.
- [ ] **Step 2: Run** `uv run pytest tests/test_image_tool.py tests/test_agent_loop.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the tool, the loop change and the registry wiring.
- [ ] **Step 4: Run the tests, then the full suite.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): image generation tool and chat attachments"`

### Task 7: Models and images API, and server wiring

**Files:**
- Create: `server/src/hearthkeep/api/routes_models.py`, `api/routes_images.py`
- Modify: `server/src/hearthkeep/api/app.py`, `api/common.py` (`Services` gains `gpu`, `images`, `cards`, `card_errors`), `server/src/hearthkeep/cli.py` (`cmd_serve` builds cards, runtime, supervisor, GPU scheduler, `ScheduledChatClient`; stops children on shutdown), `config.example.toml`/`README.md` mention `models.toml`
- Test: `server/tests/test_api_models_images.py`, `server/tests/test_cli.py`

**Interfaces:**
- Routes (session required):
  - `GET /api/models` → `{"models": [{id, name, kind, capabilities, vram_gb, source, resident: bool}], "errors": [{id_or_file, message}], "gpu": gpu.status()}`
  - `GET /api/images?before=<created_at>&limit=` → `{"images": [{id, model_id, prompt, width, height, created_at}]}` (limit 1–100, default 60)
  - `GET /api/images/{id}/file` → `image/png` bytes with `Cache-Control: private, max-age=86400`; ids must match `[A-Za-z0-9_-]{1,32}`; otherwise or unknown → 404 JSON. Uses `ImageStore.path()`.
- `cmd_serve`: if `models.toml` configures llama-server, Hearthkeep owns the chat model (writes the key file, starts it on first lease); if not, it keeps Phase 1 behaviour (expects a chat server already running at `chat_base_url`). ComfyUI is started lazily on the first image lease. On shutdown: stop children (`timeout` 10 s each). A startup line lists loaded cards and card errors (names only, no paths).
- Image `img` requests from the web app send the session cookie (same origin); CSP `img-src 'self'` already allows them.

- [ ] **Step 1: Write the failing tests**: `test_models_requires_session`, `test_models_endpoint_lists_card_errors`, `test_models_shows_resident_and_gpu_status`, `test_images_list_newest_first`, `test_image_file_served_with_png_type`, `test_image_file_rejects_unknown_and_traversal` (`/api/images/..%2F..%2Fconfig.toml/file`, `/api/images/nope/file`, an id whose file was deleted → 404), `test_image_file_requires_session`, in `test_cli.py`: `test_serve_without_models_toml_keeps_external_chat_server`, `test_serve_stops_children_on_shutdown`.
- [ ] **Step 2: Run** `uv run pytest tests/test_api_models_images.py tests/test_cli.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the routes and wiring.
- [ ] **Step 4: Run the tests, then the full suite.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(server): models and images API; Hearthkeep runs its own model servers"`

### Task 8: Images in chat and the Images view

**Files:**
- Create: `web/src/views/Images.tsx`, `web/src/components/ImageGrid.tsx`, `web/src/styles/images.css`
- Modify: `web/src/views/Chat.tsx`, `web/src/views/chatModel.ts`, `web/src/api/events.ts` (event type), `web/src/App.tsx` or `AppShell.tsx` (route `images` to the new view)
- Test: `web/src/__tests__/images.test.tsx`, `web/src/__tests__/chat.test.tsx`

**Interfaces:**
- Consumes: `attachments{step_id, items}` events (Task 6); `GET /api/images`, `GET /api/images/{id}/file` (Task 7); existing chat APIs.
- Chat: attachments render under the step as a responsive grid of thumbnails (`<img src="/api/images/{id}/file" alt="Generated image {n}">`, lazy loading); clicking opens a full-size viewer (dialog, Escape closes, arrow keys move between images, focus returns).
- Images view: header "Images"; a describe box ("Describe the image you want…") with three controls — **Model** (Auto / each image card name from `/api/models`), **Shape** (Square / Landscape / Portrait / Banner), **How many** (1–4, default 4) — and a **Create** button. Create sends a chat message to a dedicated chat titled "Images" (created on first use, its id remembered in `localStorage` key `hk.imagesChatId`, recreated if missing): `Create {n} image(s), shape {aspect}{, using <model name>}: {text}`. While the turn runs, the view shows the live tool step and the new images as they arrive (subscribe to the turn like Chat). Below, a gallery from `GET /api/images` (newest first, "Load more" pagination), each tile showing the image; the viewer shows prompt, model, size and date, with **Copy prompt**.
- Empty state: "Your images will appear here. Describe one above to start." If no image model is available (no image cards or `/api/models` errors only): "Image generation isn't set up on this PC yet." with a link to the docs section.
- Reuses tokens, motion and accessibility rules from Phase 1.

- [ ] **Step 1: Write the failing tests**: `attachments event shows thumbnails in chat`, `viewer opens, navigates with arrows and closes with Escape`, `images view sends the composed message to the Images chat`, `images view creates the Images chat once and reuses it`, `gallery paginates with Load more`, `shows not-set-up state when there are no image models`, `copy prompt copies the prompt text`.
- [ ] **Step 2: Run** `cd web && npm test`. Expected: FAIL.
- [ ] **Step 3: Implement** the view and components.
- [ ] **Step 4: Run** `npm test && npm run build`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(web): images in chat and the Images view"`

### Task 9: Models list in Tools & permissions

**Files:**
- Create: `web/src/components/ModelList.tsx`
- Modify: `web/src/views/ToolsPermissions.tsx`, `web/src/shell/TopBar.tsx` (status pill tooltip/detail shows GPU use)
- Test: `web/src/__tests__/models.test.tsx`

**Interfaces:**
- Consumes: `GET /api/models` (Task 7).
- A "Models" section above the tool list: one row per card (name, kind "Chat" / "Images", capabilities as small chips, "Shipped" or "Yours", a status dot "Loaded" (green) / "Ready" (muted)); card errors listed beneath in amber with their message; a GPU bar ("x of y GB in use"). Read-only in Phase 2 (adding a model = adding a card file; a short line links to the docs on how).
- The top-bar status pill's accessible description includes "GPU x of y GB in use" when available.

- [ ] **Step 1: Write the failing tests**: `lists models with kind and status`, `shows card errors`, `shows gpu usage`, `status pill describes gpu use`.
- [ ] **Step 2: Run** `npm test`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `npm test && npm run build`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(web): models list and GPU use"`

### Task 10: Editing the owner's own photo (FLUX.2 klein)

**Files:**
- Create: `server/src/hearthkeep/models/workflows/flux2-klein-4b-edit.json`, `server/src/hearthkeep/api/routes_uploads.py`
- Modify: `server/src/hearthkeep/images.py` (uploads), `server/src/hearthkeep/tools/images.py` (`input_image` arg), `web/src/views/Images.tsx` ("Use my photo"), `web/src/views/Chat.tsx` (attach a photo to a message)
- Test: `server/tests/test_uploads.py`, `server/tests/test_image_tool.py`, `web/src/__tests__/images.test.tsx`

**Interfaces:**
- Workflow: the API-format graph of ComfyUI's "Image Edit (FLUX.2 Klein 4B Distilled)" template (shipped with ComfyUI as `image_flux2_klein_image_edit_4b_distilled.json`; convert UI→API format), with placeholders from Task 2 plus `"{{input_image}}"` for the `LoadImage` node.
- `POST /api/uploads` (session + CSRF; multipart field `file`; PNG/JPEG/WebP only, checked by magic bytes; ≤ 15 MB; decoded dimensions ≤ 4096) → `{"id"}`; stored as `<data>/workspace/uploads/<id>.<ext>` and in `uploads`. Uploading is Look (stays on the PC). `GET /api/uploads/{id}/file` like images.
- Tool: optional `input_image` (upload id). Allowed only for cards with `image-edit` capability (else an error result naming the model to use); the tool uploads the file to ComfyUI with `comfy.upload` and uses the card's `edit_workflow`. The tool description says: "To edit or reuse the owner's photo, pass its id as input_image and use flux2-klein-4b."
- Chat: a paperclip button attaches one photo to the next message; the message text gets a line `Attached photo: {upload id}` and the photo shows as a thumbnail in the owner's bubble. Images view: **Use my photo** picks a file, uploads it, and adds "using my photo {id}" to the composed message with FLUX selected.

- [ ] **Step 1: Write the failing tests**: `test_upload_accepts_png_jpeg_webp`, `test_upload_rejects_other_types_by_content` (a `.png` name with GIF bytes → 415), `test_upload_rejects_too_large`, `test_upload_requires_session_and_csrf`, `test_edit_requires_image_edit_card`, `test_edit_uses_edit_workflow_and_uploads_input`; web: `chat attaches a photo and shows it in the bubble`, `images view use my photo selects FLUX and sends the upload id`.
- [ ] **Step 2: Run** the server and web tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suites and `npm run build`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat: edit the owner's own photo with FLUX.2 klein"`

### Task 11: Docs, real-model check and public-safety scan

**Files:**
- Create: `docs/models.md`
- Modify: `README.md`, `docs/security.md`, `scripts/smoke_e2e.py`
- Test: whole-repo privacy scan; manual run against the real models

- [ ] **Step 1: Write `docs/models.md`**: what a model card is (every field from Task 2, with the shipped Gemma card as the example), how to add your own card in `<data>/models/cards/`, how `models.toml` points at your executables and model folders (placeholders only), how GPU switching behaves (chat pauses while images render; first image after a chat takes longer), and troubleshooting (logs in `<data>/logs/`).
- [ ] **Step 2: Update** `README.md` (Phase 2 status, `models.toml` setup in the quick start) and `docs/security.md` (child processes bind to 127.0.0.1; images served only to a signed-in session; uploads type/size checks; image generation is Look).
- [ ] **Step 3: Extend** `scripts/smoke_e2e.py` with `--images`: asks "Create one square image of a red apple on a white table" and checks an `attachments` event arrives and `GET /api/images/{id}/file` returns a PNG; then asks a chat question and checks a reply arrives (chat model came back).
- [ ] **Step 4: Run the smoke test** against the real setup (`models.toml` configured, dev auth on a throwaway data folder). Expected: `OK image` and `OK chat after image`.
- [ ] **Step 5: Run** `python scripts/privacy_scan.py $(git ls-files)`, `git log -p --format= | python scripts/privacy_scan.py -`, `git log --format=%B | python scripts/privacy_scan.py -`. Expected: no findings.
- [ ] **Step 6: Commit.** `git commit -m "docs: model cards, GPU switching and images"`
- [ ] **Step 7: Stop for the owner** before any push or merge.
