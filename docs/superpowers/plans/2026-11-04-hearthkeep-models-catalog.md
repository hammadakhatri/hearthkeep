# Hearthkeep Phase 5, Project 2b: Model Catalog and Downloads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An owner with no models installed opens Settings › Chat model, sees a recommendation that fits their PC, clicks Download (one passkey), watches a resumable, checksum-verified progress list fetch llama.cpp and the model, and after one restart chats with it. They can also switch between installed models, point at a model server they already run, and download ComfyUI and the image models from Settings › Image models. Nothing is typed and nothing comes from a URL outside the catalog.

**Architecture:** A catalog file in the repo (`server/src/hearthkeep/models/catalog.toml`) lists every downloadable item with pinned URLs, sizes and SHA-256 values. `fetching.py` holds the shared HTTPS rules (host allow-list, manual redirects, size caps, SHA-256, safe unpacking), which `webbundle.py` now uses too, plus an async resumable download. `hardware.py` reads the GPU, memory and free disk and recommends a chat model and llama.cpp build. `installer.py` runs one install job at a time in the server: it downloads into `<data>/runtime/downloads`, verifies, unpacks, moves files into place under `<data>/tools` and `<data>/models`, then edits `models.toml` (and `config.toml` for an external server) through `tomledit.py`, which keeps the owner's comments, and marks "restart needed". `routes_models.py` exposes the catalog, the job and chat-model switching. The web app gets a `DownloadList` component (reused by the 2c wizard) and a Models panel in Settings. Models are still loaded at start-up only: every change asks for the existing restart.

**Tech Stack:** The Phase 1–5 stack (Python 3.12.6/uv, FastAPI, httpx, pytest, ruff; React 19 + TypeScript, Vitest). Windows `nvidia-smi`, `tar.exe` (System32 bsdtar, for ComfyUI's `.7z`) and `GlobalMemoryStatusEx` through ctypes. No new Python or npm dependency.

**Spec:** `docs/superpowers/specs/2026-11-03-hearthkeep-phase-5-design.md` §5.4 (this project), §6 "Settings › Chat model" and the "downloading" step-up line, §6a (existing `models.toml` keeps working), §12 constraints and §13 call 12. Builds on project 1 (`addons.py`, `GET /api/addons`, Settings › Add-ons) and 2a (`ServerControl.mark_restart_needed`, `RestartNotice`, `webbundle.py`, the data-folder rules). Out of scope: the wizard screens that call these APIs (2c), the start-at-login switch, remote access and advanced settings (2c), the UX pass (3), the docs rewrite beyond the sections this replaces (5).

## Global Constraints

- **Downloads only from the catalog, HTTPS, SHA-256 checked before use** (§5.4, §12). A URL never comes from a request body, a page or the model: requests name catalog item ids only. Every hop, redirects included, must be `https`, port 443 or none, no userinfo, and a host on the allow-list: exact `github.com`, exact `huggingface.co`, or a host ending in `.githubusercontent.com` or `.hf.co`.
- **Every catalog file is downloadable without signing in** (§5.4 "Sources"). Each Hugging Face URL pins a commit (`/resolve/<40-hex>/`), never `main`. The licence link is shown before the download.
- **Widening needs step-up** (§6, §12). Starting a download takes a step-up token with the new purpose `download`; pointing chat at an external server takes purpose `settings`. Cancelling, retrying an approved job and switching between installed models need only the session. All of them are audited.
- **No secrets in config files or logs** (§12). An external server's API key goes into the existing `SecretStore` under `settings.chat_api_key_secret`, never into `config.toml`, a response, a log or the audit detail.
- **Existing installs keep working** (§6a). A hand-written `models.toml` (owner paths, `-ngl 999`, `chat_base_url`) is read as before. Hearthkeep only adds or replaces the keys it manages, keeps every other line and comment, and backs the file up once as `models.toml.before-hearthkeep` before its first edit. A model the owner already has (its card files exist) shows as installed and is never downloaded again.
- **Data paths stay short.** Everything lands under the data folder (≤100 characters, 2a rule): `<data>/tools/llama.cpp/<build>`, `<data>/tools/comfyui`, `<data>/models/chat/<card-id>`, `<data>/models/image/<diffusion_models|text_encoders|vae>`. Partial files live in `<data>/runtime/downloads`.
- **Copy rules.** Owner text says "passkey", names screens ("Settings › Chat model"), and contains no CLI command, `<data>` placeholder or file path. `server/tests/copy_rules.py` `forbidden_in` and `web/src/test/copyRules.ts` `forbiddenIn` cover every new string.
- **Nothing personal in the repo; no AI attribution in commits.** Conventional Commits, no trailers.
- **Existing tests keep passing.** A test this project deliberately changes is named in its task.
- **Commands.** Server, from `server/`: `uv run pytest -q`, `uv run ruff check . ../scripts`, `uv run ruff format --check <files>`. Web, from `web/`: `npx vitest run <file>`, `npm test`, `npm run build`. `python scripts/privacy_scan.py --staged` before each commit.

## Review Focus

1. **A download is interrupted** (Wi-Fi drops, the PC sleeps, Hearthkeep restarts). Starting the same items again resumes from the `.part` file with a `Range` request and still verifies the whole file's SHA-256. A server that ignores `Range` (answers 200) restarts that file from zero instead of appending. (Task 1: `test_resume_sends_range_and_hashes_whole_file`, `test_server_ignoring_range_restarts_cleanly`; Task 5: `test_restart_mid_job_resumes_from_part`.)
2. **The disk fills up, or there isn't room to start.** Before starting, the job refuses with `no_space` and the bytes needed and free. If the disk fills mid-way, that item fails with `no_space`, its `.part` is kept for a retry, and nothing half-written is moved into place. (Task 5: `test_not_enough_space_refuses_before_starting`, `test_disk_full_mid_download_fails_item_and_keeps_part`.)
3. **The owner's hand-written `models.toml`.** Installing a model keeps their comments, their other keys and their own `[model_dirs]` entries. A Gemma they already have at their own path is shown as installed and not downloaded. A file the editor can't safely change (for example a value written as an inline table) is left untouched and the job ends `config_not_updated` with a plain message. (Task 4: `test_edit_keeps_comments_and_other_keys`, `test_unsupported_shape_is_refused_not_mangled`; Task 5: `test_owner_gemma_counts_as_installed`.)
4. **A redirect goes somewhere unexpected.** A CDN host in another region (`cas-bridge.xethub.hf.co`, `us.aws.cdn.hf.co`) is followed; `http://`, another host (`evil.example`, `huggingface.co.evil.example`, `hf.co.evil.example`), userinfo or a non-443 port stops the item with `network` and no bytes are written. (Task 1: `test_redirect_rules`.)
5. **Two clicks or two tabs.** A second `POST /api/models/install` while a job runs answers 409 `busy` with the running job, never a second job; the step-up token of the refused request is still consumed. (Task 6: `test_second_install_while_running_is_busy`.)

## Calls made in this plan

The spec leaves these open; check them while reading.

- **Chat models** (all Apache 2.0, ungated, from bartowski's GGUF re-uploads; Q4_K_M or the existing card's Q6_K):
  - `qwen3.5-4b` (new card), Qwen3.5 4B Q4_K_M, 3.0 GB: runs on CPU or under 6 GB of graphics memory.
  - `qwen3.5-9b` (new card), Qwen3.5 9B Q4_K_M, 6.2 GB: for about 8 GB.
  - `gemma-4-12b` (the existing card, unchanged), Gemma 4 12B Q6_K 10.2 GB plus its F16 vision projector 0.12 GB, saved under the card's file names: for 11 GB or more. Owners who installed Gemma by hand keep their files.
- **llama.cpp b12099 for Windows x64**: the CUDA 12.4 build with its `cudart` runtime zip (NVIDIA), the Vulkan build (other GPUs) and the CPU build. One build is installed; the recommendation picks CUDA when `nvidia-smi` answers, otherwise CPU. Vulkan is offered as an alternative, never recommended (no reliable detection).
- **Recommendation rule:** the largest chat model whose card `vram_gb` ≤ detected VRAM; with no NVIDIA GPU, `qwen3.5-4b` on the CPU build. VRAM is `gpu_vram_gb` from `models.toml` when set, else `nvidia-smi`.
- **ComfyUI portable v0.44.0, NVIDIA CUDA 12.6 build** (`.7z`, 1.9 GB), unpacked with Windows' own `tar.exe` (bsdtar reads 7-Zip) into a staging folder, then checked for links and moved into place. Images need an NVIDIA GPU; without one the image items say so and can't be chosen.
- **Image models** from Comfy-Org's ungated repackages (Apache 2.0): Z-Image Turbo (`z_image_turbo_bf16`, `qwen_3_4b`, `ae`) and FLUX.2 klein 4B (`flux-2-klein-4b`, the shared `qwen_3_4b`, `flux2-vae`). A file shared by two models is downloaded once (same SHA-256).
- **One job at a time**, held in memory by the server. A restart forgets the job but keeps `.part` files, so starting the same items again resumes. A failed file is retried automatically up to 3 times (network errors only) before the item fails; a checksum mismatch deletes the file and fails the item at once.
- **`models.toml` gains one key, `chat_server = "local" | "external"`.** Unset means today's behaviour (local wins when it's fully configured). `"external"` makes `build_model_stack` use `chat_base_url` even when local models are installed, so switching back and forth never deletes anything.
- **Editing TOML in place, no new dependency.** `tomledit.py` changes only top-level `key = value` lines and `[model_dirs]` entries with basic strings, numbers or booleans, appends what's missing, removes keys on request, then re-parses the result with `tomllib` and checks the values; anything else raises `TomlEditError` and the file is left as it was.
- **Restart, not reload.** After a successful job or a chat switch, the server calls `control.mark_restart_needed("models")`; the existing `RestartNotice` offers "Restart now" (also shown in the new panel). Unsupervised (developers running `serve`), the panel says "Restart Hearthkeep to use it."
- **Step-up purpose `download`** joins the existing list in `api/routes_auth.py` and `web/src/api/webauthn.ts`.
- **The pin script** `scripts/pin_catalog.py` fills and checks sizes and SHA-256 values from the GitHub releases API (`digest`) and the Hugging Face tree API (`lfs.oid`) without downloading the files. It runs by hand when the catalog changes, never in CI.

---

## File structure

```
server/src/hearthkeep/fetching.py              # NEW: HTTPS allow-list, redirects, async resumable download, sha256, safe unpack
server/src/hearthkeep/webbundle.py             # MODIFY: use fetching.py (remove its copies)
server/src/hearthkeep/models/catalog.toml      # NEW: the catalog
server/src/hearthkeep/models/catalog.py        # NEW: load + validate the catalog
server/src/hearthkeep/models/cards/qwen3.5-4b.toml   # NEW
server/src/hearthkeep/models/cards/qwen3.5-9b.toml   # NEW
server/src/hearthkeep/models/runtime.py        # MODIFY: chat_server key
server/src/hearthkeep/hardware.py              # NEW: PC facts + recommendation
server/src/hearthkeep/runtime/gpu.py           # MODIFY: nvidia-smi name + memory query shared with hardware.py
server/src/hearthkeep/tomledit.py              # NEW: in-place TOML key editor
server/src/hearthkeep/installer.py             # NEW: install jobs
server/src/hearthkeep/api/routes_models.py     # MODIFY: today only GET /api/models; add the catalog, job and chat routes
server/src/hearthkeep/api/routes_auth.py       # MODIFY: step-up purpose "download"
server/src/hearthkeep/api/app.py               # MODIFY: Services.installer, router, lifespan shutdown
server/src/hearthkeep/cli.py                   # MODIFY: build_model_stack honours chat_server
server/src/hearthkeep/addons.py                # MODIFY: chat/images actions point at the new panel
scripts/pin_catalog.py                         # NEW
web/src/api/models.ts                          # MODIFY: catalog, job, chat switch calls
web/src/api/webauthn.ts                        # MODIFY: purpose "download"
web/src/components/DownloadList.tsx            # NEW: progress list (reused by 2c)
web/src/components/ModelsPanel.tsx             # NEW: Settings › Chat model and Image models
web/src/views/Settings.tsx                     # MODIFY: add the panel
web/src/shell/AddonsContext.tsx                # MODIFY: anchors settings-chat, settings-images; action targets
web/src/components/ModelList.tsx               # MODIFY: its "add a card file" line points at Settings › Chat model
docs/install.md, docs/models.md, README.md, server/src/hearthkeep/config.example.toml   # MODIFY (Task 8)
```

---

### Task 1: Shared HTTPS fetching and a resumable download

**Files:**
- Create: `server/src/hearthkeep/fetching.py`
- Modify: `server/src/hearthkeep/webbundle.py` (`_allowed_url`, `_sha256_file`, `_check_entry`, `safe_extract` move out; `download` uses the shared redirect walk)
- Test: `server/tests/test_fetching.py`; `server/tests/test_webbundle.py` keeps passing unchanged except imports

**Interfaces:**
- Produces:
  - `class FetchError(Exception)` with `reason: Literal["network", "not_found", "too_big", "checksum", "no_space", "cancelled", "unpack"]` and a `detail: str` (never shown to the owner).
  - `allowed_url(url: httpx.URL, *, exact: frozenset[str], suffixes: tuple[str, ...]) -> bool`
  - `MODEL_HOSTS = frozenset({"github.com", "huggingface.co"})`, `MODEL_HOST_SUFFIXES = (".githubusercontent.com", ".hf.co")`. `webbundle` keeps its own exact set and no suffixes.
  - `sha256_file(path: Path) -> str` (lowercase hex).
  - `safe_extract_zip(zip_path: Path, dest: Path, *, max_files: int, max_bytes: int) -> None` (today's checks, caps as parameters; `webbundle` passes its current constants).
  - `@dataclass class Progress: done: int; total: int`
  - `async def download_file(url: str, part: Path, *, size: int, sha256: str, client: httpx.AsyncClient, on_progress: Callable[[Progress], None], cancelled: Callable[[], bool], hosts: frozenset[str] = MODEL_HOSTS, suffixes: tuple[str, ...] = MODEL_HOST_SUFFIXES) -> None`
    - resumes when `part` exists and is shorter than `size` (`Range: bytes=<n>-`, expects 206 and a matching `Content-Range`); a 200 answer truncates `part` and starts over; `part` longer than `size` is deleted and started over;
    - walks redirects by hand (at most 5), checking each hop with `allowed_url`;
    - stops with `too_big` past `size` bytes, `cancelled` when `cancelled()` turns true (part kept), `no_space` on `OSError` with `errno.ENOSPC` or Windows error 112 (part kept);
    - when `size` bytes are in, hashes the whole `part` (`sha256_file`); a mismatch deletes `part` and raises `checksum`; success leaves the verified `part` for the caller to move.
  - `def new_client() -> httpx.AsyncClient` with `trust_env=False, follow_redirects=False, timeout=httpx.Timeout(30, read=60)`.

- [ ] **Step 1: Write the failing tests** in `test_fetching.py`, all with `httpx.MockTransport`, no network:
  - `test_download_writes_and_verifies` — 200 with the body; `part` holds it; progress callbacks end at `Progress(size, size)`.
  - `test_resume_sends_range_and_hashes_whole_file` — `part` already holds the first half; the handler asserts `request.headers["Range"] == f"bytes={half}-"` and answers 206 with `Content-Range: bytes {half}-{size-1}/{size}`; the result verifies against the SHA-256 of the whole body.
  - `test_server_ignoring_range_restarts_cleanly` — same start, the handler answers 200 with the full body; `part` equals the body (not half + body).
  - `test_redirect_rules` — parametrised: redirect to `https://cas-bridge.xethub.hf.co/x` and `https://us.aws.cdn.hf.co/x` and `https://release-assets.githubusercontent.com/x` succeed; to `http://huggingface.co/x`, `https://evil.example/x`, `https://huggingface.co.evil.example/x`, `https://hf.co.evil.example/x`, `https://user@huggingface.co/x`, `https://huggingface.co:8443/x` raise `FetchError("network")` with no `part` written; 6 redirects raise `network`.
  - `test_checksum_mismatch_deletes_part` — `reason == "checksum"`, `part` gone.
  - `test_too_big_stops` — the server sends more than `size`: `reason == "too_big"`.
  - `test_cancel_keeps_part` — `cancelled` turns true after the first chunk: `reason == "cancelled"`, `part` exists with the bytes so far.
  - `test_disk_full_keeps_part` — a fake writer raising `OSError(errno.ENOSPC, ...)`: `reason == "no_space"`, `part` kept.
  - `test_404_is_not_found`.
  - `test_safe_extract_zip_caps_are_parameters` — a zip with 3 files and `max_files=2` raises; `max_bytes` likewise.

- [ ] **Step 2: Run** `uv run pytest -q tests/test_fetching.py` — expected: FAIL (module missing).

- [ ] **Step 3: Implement `fetching.py`** with the signatures above. Write in 1 MiB chunks; call `on_progress` at most every 0.25 s plus once at the end. Then point `webbundle.py` at `allowed_url`, `sha256_file` and `safe_extract_zip` and delete its private copies (`_allowed_url`, `_sha256_file`, `_check_entry`, the body of `safe_extract`), keeping `webbundle.safe_extract(zip_path, dest)` as a one-line wrapper so its callers and tests don't change.

- [ ] **Step 4: Run** `uv run pytest -q tests/test_fetching.py tests/test_webbundle.py` — expected: PASS.

- [ ] **Step 5: Commit** `feat(fetching): shared HTTPS rules and a resumable, verified download`.

---

### Task 2: The catalog and the new chat cards

**Files:**
- Create: `server/src/hearthkeep/models/catalog.toml`, `server/src/hearthkeep/models/catalog.py`, `server/src/hearthkeep/models/cards/qwen3.5-4b.toml`, `server/src/hearthkeep/models/cards/qwen3.5-9b.toml`, `scripts/pin_catalog.py`
- Test: `server/tests/test_catalog.py`, `server/tests/test_pin_catalog.py`

**Interfaces:**
- Consumes: `load_cards`, `ModelCard`, `IMAGE_FILE_FOLDERS` (models/cards.py, models/runtime.py); `allowed_url`, `MODEL_HOSTS`, `MODEL_HOST_SUFFIXES` (Task 1).
- Produces:
  - `@dataclass(frozen=True) class CatalogFile: url: str; size: int; sha256: str; unpack: Literal["", "zip", "7z"] = ""; save_as: str = ""`
  - `@dataclass(frozen=True) class CatalogItem: id: str; kind: Literal["runtime", "chat", "comfyui", "image"]; name: str; licence: str; licence_url: str; files: tuple[CatalogFile, ...]; variant: Literal["", "cuda", "vulkan", "cpu"] = ""; card: str = ""; needs_nvidia: bool = False; install_dir: str = ""; exe: str = ""; python: str = ""; main: str = ""; note: str = ""`, with `size_bytes` (property, sum of file sizes).
  - `load_catalog() -> dict[str, CatalogItem]` (ordered as in the file; raises `CatalogError(message)` on any rule below).
  - Item ids used by later tasks: `llama-cuda`, `llama-vulkan`, `llama-cpu`, `chat-qwen3.5-4b`, `chat-qwen3.5-9b`, `chat-gemma-4-12b`, `comfyui`, `image-z-image-turbo`, `image-flux2-klein-4b`.

The catalog format (one item shown; every item follows it):

```toml
schema = 1

[[item]]
id = "chat-qwen3.5-9b"
kind = "chat"
name = "Qwen3.5 9B"
card = "qwen3.5-9b"
licence = "Apache 2.0"
licence_url = "https://huggingface.co/Qwen/Qwen3.5-9B"
note = "Good all-rounder for about 8 GB of graphics memory."

[[item.file]]
url = "https://huggingface.co/bartowski/Qwen_Qwen3.5-9B-GGUF/resolve/182be2fd6c7bc44887d88a91cb03ff009cc9f549/Qwen_Qwen3.5-9B-Q4_K_M.gguf"
size = 6169341984
sha256 = "d784ce9eda1a5a7b51e8f705a9e6310844bf4f173654d115823c775fdea56d43"
save_as = "model"
```

Pinned values (verified on 2026-11-04 from the APIs; Task 2's pin script re-checks them):

| Item | File URL (abbreviated) | Size | SHA-256 |
|---|---|---|---|
| `llama-cuda` | `github.com/ggml-org/llama.cpp/releases/download/b12099/llama-b12099-bin-win-cuda-12.4-x64.zip` (`unpack = "zip"`) | 287012644 | `6c45ab354ade102e96d344168598d552b1fb6f8b1cbb984753e4d0acaddee017` |
| `llama-cuda` | `…/b12099/cudart-llama-bin-win-cuda-12.4-x64.zip` (`unpack = "zip"`) | 391443627 | `8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6` |
| `llama-vulkan` | `…/b12099/llama-b12099-bin-win-vulkan-x64.zip` | 37660003 | `a3c901e3289eff7bfbe1315e855bba3026ef03929c88f6104a1d9bf8b093450b` |
| `llama-cpu` | `…/b12099/llama-b12099-bin-win-cpu-x64.zip` | 24706916 | `ba751a91caeb8f5492c408386a46c9e0eba7da599c81bce1383260342e2b672b` |
| `chat-qwen3.5-4b` | `huggingface.co/bartowski/Qwen_Qwen3.5-4B-GGUF/resolve/4168f45a16a1290d65a4ec0fa312ae917a4c15d6/Qwen_Qwen3.5-4B-Q4_K_M.gguf` (`save_as = "model"`) | 3013027808 | `13c16f426047e2de38cd075bdade4a7bcbc8c774384876f677740cda65f8a983` |
| `chat-qwen3.5-9b` | as above | 6169341984 | `d784ce9eda1a5a7b51e8f705a9e6310844bf4f173654d115823c775fdea56d43` |
| `chat-gemma-4-12b` | `huggingface.co/bartowski/gemma-4-12B-it-GGUF/resolve/2ae7d41be21ca62de00a2d320ee9cec50daa3aa6/gemma-4-12B-it-Q6_K.gguf` (`save_as = "model"`) | 10239326688 | `53336de05c4af36dcb61daf0d984be076ddf0245e3ce7f61aa10b72cb4044e97` |
| `chat-gemma-4-12b` | `…/2ae7d41be21ca62de00a2d320ee9cec50daa3aa6/mmproj-gemma-4-12B-it-f16.gguf` (`save_as = "mmproj"`) | 122031552 | `563192209f002b0a13af16a4992fdb9dd61187a36919eaf65f408bb47af3d272` |
| `comfyui` | `github.com/comfyanonymous/ComfyUI/releases/download/v0.44.0/ComfyUI_windows_portable_nvidia_cu126.7z` (`unpack = "7z"`) | 1945636677 | `a26cf84f043b3d9fbf0d53296f57d7190482c339caf1b3b5cfda5149d454be9f` |
| `image-z-image-turbo` | `huggingface.co/Comfy-Org/Z-Image-Turbo/resolve/6fc90a3b1b653e935a0d175e260736de25b84df5/split_files/diffusion_models/z_image_turbo_bf16.safetensors` (`save_as = "unet"`) | 12309866400 | `2407613050b809ffdff18a4ac99af83ea6b95443ecebdf80e064a79c825574a6` |
| `image-z-image-turbo` | `…/split_files/text_encoders/qwen_3_4b.safetensors` (`save_as = "text_encoder"`) | 8044982048 | `6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a` |
| `image-z-image-turbo` | `…/split_files/vae/ae.safetensors` (`save_as = "vae"`) | 335304388 | `afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38` |
| `image-flux2-klein-4b` | `huggingface.co/Comfy-Org/Flux-2-Klein-4B/resolve/5b3abca5c0dc1ecd4eaf3340868d09063fc1c965/split_files/diffusion_models/flux-2-klein-4b.safetensors` (`save_as = "unet"`) | 7751105712 | `ec3d4e733a771f61c052fb4856c48b336c55eaf2c65487c2a1faeb9bbda7a343` |
| `image-flux2-klein-4b` | `…/split_files/text_encoders/qwen_3_4b.safetensors` (`save_as = "text_encoder"`) | 8044982048 | `6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a` |
| `image-flux2-klein-4b` | `…/split_files/vae/flux2-vae.safetensors` (`save_as = "vae"`) | 336211292 | `868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3` |

Per-kind fields: runtimes set `variant`, `install_dir = "tools/llama.cpp/b12099-<variant>"`, `exe = "llama-server.exe"`, and `needs_nvidia = true` for CUDA; `comfyui` sets `install_dir = "tools/comfyui"`, `python = "ComfyUI_windows_portable/python_embeded/python.exe"`, `main = "ComfyUI_windows_portable/ComfyUI/main.py"`, `needs_nvidia = true`; image items set `card` and `needs_nvidia = true`. Licence links: llama.cpp `https://github.com/ggml-org/llama.cpp/blob/b12099/LICENSE` (MIT), ComfyUI `https://github.com/comfyanonymous/ComfyUI/blob/v0.44.0/LICENSE` (GPL-3.0), Qwen `https://huggingface.co/Qwen/Qwen3.5-4B` and `…-9B`, Gemma `https://huggingface.co/google/gemma-4-12B-it`, Z-Image `https://huggingface.co/Tongyi-MAI/Z-Image-Turbo`, FLUX.2 klein `https://huggingface.co/black-forest-labs/FLUX.2-klein-4B` (each Apache 2.0).

New cards (same shape as `gemma-4-12b.toml`; `capabilities = ["tools", "thinking"]`, no `mmproj`, `launch_args` with `--model {model}`, `--alias <id>`, `--host 127.0.0.1`, `--port {port}`, `--api-key-file {api_key_file}`, `-c <context>`, `-ngl 999`, `--jinja`; sampling `temperature = 0.6, top_k = 20, top_p = 0.95, min_p = 0.0`):
- `qwen3.5-4b`: name "Qwen3.5 4B", `vram_gb = 4.0`, `context = 16384`, file `model = "Qwen_Qwen3.5-4B-Q4_K_M.gguf"`.
- `qwen3.5-9b`: name "Qwen3.5 9B", `vram_gb = 7.5`, `context = 16384`, file `model = "Qwen_Qwen3.5-9B-Q4_K_M.gguf"`.

Catalog rules `load_catalog` enforces (each with a test):
- ids unique and match the card id pattern; `kind` known; at least one file per item;
- every URL passes `allowed_url(..., exact=MODEL_HOSTS, suffixes=MODEL_HOST_SUFFIXES)` and, on `huggingface.co`, contains `/resolve/<40 hex>/`;
- `sha256` is 64 lowercase hex; `size > 0`; `licence_url` is https;
- chat and image items name a shipped card of the same kind, and their `save_as` values are exactly that card's `files` keys;
- runtime items have a `variant`, `install_dir` and `exe`; `comfyui` has `install_dir`, `python` and `main`; `install_dir` is relative, uses `/`, and has no `..`;
- the three chat sizes the spec asks for exist: at least one chat card with `vram_gb ≤ 6`, one in `(6, 9]`, and `gemma-4-12b`.

- [ ] **Step 1: Write the failing tests** in `test_catalog.py`: `test_shipped_catalog_loads` (all 9 ids, `chat-gemma-4-12b.size_bytes == 10239326688 + 122031552`), `test_chat_items_save_as_card_file_names`, `test_spec_sizes_are_covered`, and one parametrised `test_catalog_rule_refuses` that feeds `load_catalog` a broken copy (through a `text` parameter: `load_catalog(text: str | None = None)`) for each rule above. Also in `test_model_cards.py`: `test_qwen_cards_load` (both new cards parse with the values above).

- [ ] **Step 2: Run** them — expected: FAIL.

- [ ] **Step 3: Write `catalog.toml`, the two cards and `catalog.py`.**

- [ ] **Step 4: Write `scripts/pin_catalog.py`**: `python scripts/pin_catalog.py --check` reads the catalog and, for each file, asks the GitHub releases API (`digest` of the asset with that name) or the Hugging Face tree API at the pinned commit (`size`, `lfs.oid`) and prints one line per file: `ok` or `differs: size … sha256 …`, exit 1 if any differ. `--write` rewrites `size`/`sha256` lines in place. It takes `--api` injection for tests. `test_pin_catalog.py`: `test_check_reports_ok_and_differences` with a fake API returning one matching and one different file. The script is never run in CI.

- [ ] **Step 5: Run** `uv run pytest -q tests/test_catalog.py tests/test_model_cards.py tests/test_pin_catalog.py`, then run `python scripts/pin_catalog.py --check` once by hand (network) and confirm every line says `ok`. Expected: PASS and all `ok`.

- [ ] **Step 6: Commit** `feat(models): a pinned download catalog and two smaller chat models`.

---

### Task 3: PC facts and the recommendation

**Files:**
- Create: `server/src/hearthkeep/hardware.py`
- Modify: `server/src/hearthkeep/runtime/gpu.py` (one `nvidia_smi_query()` used by both `detect_vram_gb` and `hardware.py`)
- Test: `server/tests/test_hardware.py`

**Interfaces:**
- Consumes: `load_catalog`, `CatalogItem` (Task 2); `load_cards`; `load_runtime` (for `gpu_vram_gb`).
- Produces:
  - `nvidia_smi_query(*, run=subprocess.run) -> tuple[str, float] | None` in `gpu.py`: `(name, vram_gb)` of the first GPU from `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits`; `detect_vram_gb()` becomes a thin wrapper (its tests keep passing).
  - `@dataclass(frozen=True) class PcInfo: gpu_name: str | None; vram_gb: float | None; nvidia: bool; ram_gb: float | None; free_disk_gb: float`; `to_json()`.
  - `read_pc(data_dir: Path, *, smi=nvidia_smi_query, memory=total_memory_gb, disk=shutil.disk_usage) -> PcInfo` — `vram_gb` from `models.toml` `gpu_vram_gb` when set, else the query; `free_disk_gb` of the data folder's drive.
  - `total_memory_gb() -> float | None` (`GlobalMemoryStatusEx` on Windows, `None` elsewhere).
  - `@dataclass(frozen=True) class Recommendation: chat: str; runtime: str; reason: Literal["fits_gpu", "no_nvidia", "small_gpu"]`
  - `recommend(pc: PcInfo, catalog: dict[str, CatalogItem], cards: dict[str, ModelCard]) -> Recommendation`
  - `fits(item: CatalogItem, pc: PcInfo, cards) -> bool` — chat: card `vram_gb ≤ pc.vram_gb`, or the item is the smallest chat model; image and `comfyui`: `pc.nvidia`; runtime: CUDA needs `pc.nvidia`, others always.

- [ ] **Step 1: Write the failing tests**:
  - `test_recommend_by_vram` — parametrised: `(vram, nvidia) → (chat, runtime)`: `(24, True) → ("chat-gemma-4-12b", "llama-cuda")`, `(12, True) → gemma`, `(11.0, True) → gemma`, `(10.9, True) → qwen3.5-9b`, `(8, True) → qwen3.5-9b`, `(6, True) → qwen3.5-4b`, `(None, False) → ("chat-qwen3.5-4b", "llama-cpu")` with `reason == "no_nvidia"`.
  - `test_gpu_vram_gb_in_models_toml_wins` — `models.toml` says 16, the query says 8: `pc.vram_gb == 16`.
  - `test_nvidia_smi_missing_or_failing` — `run` raising `FileNotFoundError`, returning code 1, or printing garbage gives `None`, never raises.
  - `test_nvidia_smi_parses_name_and_memory` — `"NVIDIA GeForce RTX 4080, 16376\n"` → `("NVIDIA GeForce RTX 4080", 15.99…)` (MiB / 1024).
  - `test_fits_rules` — images need NVIDIA; CUDA needs NVIDIA; the smallest chat model always fits.

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest -q tests/test_hardware.py tests/test_gpu.py` — PASS.

- [ ] **Step 5: Commit** `feat(hardware): read the PC and recommend a chat model`.

---

### Task 4: Editing `models.toml` and `config.toml` in place

**Files:**
- Create: `server/src/hearthkeep/tomledit.py`
- Modify: `server/src/hearthkeep/models/runtime.py` (`chat_server` key: `_STR_KEYS` gains it; `RuntimeConfig.chat_server: Literal["", "local", "external"] = ""`; any other value is a `ConfigError`)
- Test: `server/tests/test_tomledit.py`, `server/tests/test_model_cards.py` (where `load_runtime` is tested)

**Interfaces:**
- Produces:
  - `class TomlEditError(Exception)` — the file is untouched when it's raised.
  - `def edit_toml(path: Path, *, set_top: Mapping[str, str | int | float | bool] = {}, remove_top: Iterable[str] = (), set_table: Mapping[str, Mapping[str, str]] = {}, backup: Path | None = None) -> None`
    - replaces the value of an existing top-level `key = value` line (keeping a trailing `# comment`), appends missing top-level keys before the first table header, removes listed top-level keys;
    - in a named table (`[model_dirs]`), replaces or appends `"key" = 'value'` entries, creating the table at the end of the file if it's missing;
    - writes path strings as TOML literal strings (`'C:\Hearthkeep\data\…'`), or basic strings with escapes when the value contains `'`;
    - refuses (`TomlEditError`) a key that's present as a dotted key, inside an inline table, in an array of tables, or more than once;
    - re-parses the result with `tomllib` and checks every requested change and that no other key changed, before writing atomically (`.tmp` + `os.replace`);
    - copies the original to `backup` first when `backup` is given and doesn't exist yet;
    - a missing file is created.

- [ ] **Step 1: Write the failing tests**:
  - `test_edit_keeps_comments_and_other_keys` — start from `server/tests/fixtures/upgrade/models.toml` plus a comment line and a commented key; set `llama_server_exe`, add `[model_dirs] "qwen3.5-4b"`, set `default_chat_model`; the comment, the owner's `"my-gemma"` entry and `llama_port` are byte-identical afterwards; `tomllib` reads the new values.
  - `test_missing_file_is_created`, `test_remove_top_key`, `test_backup_written_once`.
  - `test_windows_path_round_trips` — `C:\Hearthkeep\data\models\chat\qwen3.5-4b` and a path containing `'` both read back exactly.
  - `test_unsupported_shape_is_refused_not_mangled` — parametrised over a dotted key (`model_dirs.x = '…'`), an inline table (`model_dirs = { x = '…' }`), a duplicate key; each raises `TomlEditError` and the file's bytes are unchanged.
  - `test_chat_server_key` (runtime): `"external"` and `"local"` load; `"remote"` is a `ConfigError`; missing is `""`.

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (a line-based editor; no TOML writer dependency). **Step 4: Run** `uv run pytest -q tests/test_tomledit.py tests/test_model_cards.py` — PASS.

- [ ] **Step 5: Commit** `feat(config): edit models.toml and config.toml without losing the owner's lines`.

---

### Task 5: Install jobs

**Files:**
- Create: `server/src/hearthkeep/installer.py`
- Modify: `server/src/hearthkeep/cli.py` (`build_model_stack`: when `rt.chat_server == "external"` and `settings.chat_base_url_set`, skip owned mode with no warning)
- Test: `server/tests/test_installer.py`, `server/tests/test_cli.py` (one new test)

**Interfaces:**
- Consumes: Task 1 (`download_file`, `FetchError`, `safe_extract_zip`, `sha256_file`, `new_client`), Task 2 (`CatalogItem`, `load_catalog`), Task 3 (`read_pc`, `fits`), Task 4 (`edit_toml`, `TomlEditError`), `load_runtime`, `load_cards`, `card_file_paths`, `IMAGE_FILE_FOLDERS`, `procutil.system32_exe`, `ServerControl.mark_restart_needed`.
- Produces:
  - `ItemState = Literal["queued", "downloading", "verifying", "unpacking", "done", "failed", "cancelled"]`
  - `@dataclass class ItemProgress: id: str; name: str; state: ItemState; done_bytes: int; total_bytes: int; speed_bps: float; error: str | None` (`error` is one of `FetchError.reason` values or `"config_not_updated"`).
  - `@dataclass class Job: id: str; items: list[ItemProgress]; state: Literal["running", "done", "failed", "cancelled"]; make_default: bool`; `to_json()` → `{"id", "state", "items": [...], "restart_needed": bool}`.
  - `class Installer(data_dir: Path, control: ServerControl, *, catalog=None, client_factory=new_client, run=subprocess.run, disk=shutil.disk_usage, clock=time.monotonic)`
    - `installed(item: CatalogItem) -> bool` — runtime: `models.toml`'s `llama_server_exe` exists and lives under this item's `install_dir`, or `<data>/<install_dir>/<exe>` exists; chat/image: `card_file_paths(card, rt)` all exist (so an owner's own Gemma counts); comfyui: its `python` and `main` exist under `install_dir` or at `models.toml`'s paths.
    - `plan(ids: list[str]) -> list[CatalogItem]` — expands dependencies: a chat item adds the recommended runtime when no runtime is installed; an image item adds `comfyui` when it isn't installed; installed items are dropped; unknown ids raise `KeyError`.
    - `needed_bytes(items) -> int` — remaining bytes (sizes minus verified files and `.part` lengths), counting a file shared by two items once (same `sha256`), plus each archive's size again for unpacking.
    - `start(ids: list[str], *, make_default: bool) -> Job` — raises `InstallBusy(job)` while a job runs and `NotEnoughSpace(needed, free)` when `needed_bytes × 1.1` exceeds free space; otherwise starts an `asyncio` task and returns the job.
    - `current() -> Job | None`, `cancel() -> None`, `retry() -> Job` (restarts only the failed and cancelled items of the last job; `InstallBusy` while running), `async shutdown()`.
  - Files go to `<data>/runtime/downloads/<sha256>.part`; a verified file is moved (`os.replace`) to its place:
    - chat: `<data>/models/chat/<card>/<card.files[save_as]>`;
    - image: `<data>/models/image/<IMAGE_FILE_FOLDERS[save_as]>/<card.files[save_as]>`;
    - runtime and comfyui archives: unpacked into `<data>/runtime/downloads/<item>.unpack`, then that folder replaces `<data>/<install_dir>` (`.old` rollback as in `webbundle._swap_in`). Zip uses `safe_extract_zip(max_files=5000, max_bytes=4 GiB)`; 7z runs `system32_exe("tar.exe") -xf <archive> -C <staging>` (timeout 30 min, no window), then walks the result and fails with `unpack` on any symlink, junction or reparse point, and checks `python`/`main` exist.
  - After every item of a job is `done`, it updates `models.toml` once with `edit_toml(..., backup=<data>/models.toml.before-hearthkeep)`:
    - runtime: `llama_server_exe = <abs exe>`;
    - chat: `[model_dirs] "<card>" = <abs dir>`; `default_chat_model = <card>` and `chat_server = "local"` when `make_default`;
    - comfyui: `comfyui_python`, `comfyui_main`, `comfyui_model_root = <data>/models/image`;
    - image: `default_image_model = <card>` when the current default's files are missing;
    then `control.mark_restart_needed("models")`. A `TomlEditError` ends the job `failed` with `error = "config_not_updated"` on the last item (files stay in place, so a retry only redoes the edit).
  - Audit (through a `record` callback passed in by the API layer, so `installer.py` stays free of the API): `models.install_started` `{items, bytes}`, `models.install_finished` `{items, state}`. No URL or path in the detail.

- [ ] **Step 1: Write the failing tests** (fake catalog with tiny files served by `httpx.MockTransport`; a fake `run` for `tar.exe` that creates the expected tree; a fake `disk`):
  - `test_install_chat_model_end_to_end` — `start(["chat-qwen3.5-4b"], make_default=True)` on an empty data folder also installs the recommended runtime (fake PC: no NVIDIA → `llama-cpu`); afterwards the files are in place, `models.toml` has `llama_server_exe`, `[model_dirs]`, `default_chat_model = "qwen3.5-4b"`, `chat_server = "local"`, and `control.restart_needed == {"models"}`.
  - `test_owner_gemma_counts_as_installed` — a `models.toml` whose `[model_dirs] "gemma-4-12b"` points at a temp folder holding both files: `installed(chat-gemma-4-12b)` is true and `plan(["chat-gemma-4-12b"])` is empty.
  - `test_shared_image_file_downloads_once` — both image items in one job: the `qwen_3_4b` URL is requested once and the file exists in `text_encoders`.
  - `test_not_enough_space_refuses_before_starting` — `NotEnoughSpace` carries `needed` and `free`; no request made.
  - `test_disk_full_mid_download_fails_item_and_keeps_part` — the item ends `failed`/`no_space`, the `.part` stays, nothing moved into `models/`.
  - `test_network_error_retries_three_times_then_fails` and `test_checksum_mismatch_fails_at_once`.
  - `test_restart_mid_job_resumes_from_part` — a first `Installer` is shut down mid-file; a new `Installer` started with the same ids sends `Range` for the remaining bytes.
  - `test_cancel_then_retry` — `cancel()` ends the job `cancelled`; `retry()` finishes it.
  - `test_7z_with_a_link_is_refused` — the fake `tar` creates a symlink: `unpack`, nothing moved into `tools/`.
  - `test_unsupported_models_toml_ends_config_not_updated` — an inline-table `model_dirs`: job `failed`, error `config_not_updated`, model files in place, `models.toml` bytes unchanged.
  - `test_busy` — a second `start` while running raises `InstallBusy`.
  - In `test_cli.py`: `test_chat_server_external_uses_chat_base_url` — owned models plus `chat_server = "external"` and `chat_base_url` set gives chat mode `external` with no warning.

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** Speed is an exponential moving average over 2 s. **Step 4: Run** `uv run pytest -q tests/test_installer.py tests/test_cli.py` — PASS.

- [ ] **Step 5: Commit** `feat(installer): resumable, verified model installs that update models.toml`.

---

### Task 6: The models API and chat switching

**Files:**
- Modify: `server/src/hearthkeep/api/routes_models.py` (today it has only `GET /api/models`; keep that response unchanged and add the routes below)
- Modify: `server/src/hearthkeep/api/routes_auth.py` (`StepUpPurpose` gains `"download"`), `server/src/hearthkeep/api/app.py` (`Services.installer`, router registration, `await installer.shutdown()` in the lifespan `finally`), `server/src/hearthkeep/addons.py` (copy and actions, see below)
- Test: `server/tests/test_api_model_install.py` (new; `test_api_models_images.py` already exists and keeps passing), `server/tests/test_owner_copy.py`

**Interfaces:**
- Consumes: Tasks 2–5; `services`, `ApiError`, `record`, `rate_limit`, `SessionDep`, `hk.step_up.check` (as in `routes_server._require_step_up`); `SecretStore`; `tomledit.edit_toml`; config's loopback check for `chat_base_url` (reuse the validator in `config.py`, don't copy it).
- Produces (all under `/api/models`, all need a session; unsafe methods go through the existing CSRF middleware; rate-limit group `"models"`):
  - `GET /api/models/catalog` → `{"pc": PcInfo.to_json(), "recommended": {"chat", "runtime", "reason"}, "items": [{"id", "kind", "name", "note", "licence", "licence_url", "size_bytes", "needs_nvidia", "variant", "card", "installed", "fits"}], "chat": {"source": "local" | "external" | "none", "model": str | null, "base_url": str | null}, "restart_needed": bool, "supervised": bool}`. `read_pc` runs in a thread and is cached for 60 s.
  - `POST /api/models/install` body `{"items": list[str] (1–10 ids, each ≤ 64 chars), "make_default": bool = true, "step_up_token": str}` → 202 `{"job"}`. Step-up purpose `download` (checked before anything else; a missing or wrong token is 403 `step_up_required` and audits `models.install_denied`). 400 `unknown_item`; 400 `doesnt_fit` for an item whose `fits` is false; 409 `busy` `{"job"}`; 409 `no_space` `{"needed_bytes", "free_bytes"}`; 409 `nothing_to_do` when everything is installed.
  - `GET /api/models/install` → `{"job": Job.to_json() | null}`.
  - `POST /api/models/install/cancel` → 200 `{"job"}` (session only).
  - `POST /api/models/install/retry` → 202 `{"job"}`; 409 `busy`; 404 `no_job` (session only: the items were approved with a passkey when the job started).
  - `POST /api/models/chat` body `{"source": "local", "card": str}` or `{"source": "external", "base_url": str (≤ 200), "api_key": str | null (≤ 512), "step_up_token": str}`:
    - local: 400 `not_installed` unless the card's files exist; sets `default_chat_model` and `chat_server = "local"`; session only; audits `models.chat_switched {card}`.
    - external: step-up purpose `settings`; 400 `bad_url` unless it passes the same loopback rule as `config.toml`; writes `chat_base_url` to `config.toml` and `chat_server = "external"` to `models.toml`; stores `api_key` in `SecretStore` (empty string deletes it; `null` leaves it); audits `models.chat_external_set {host}` (host only, never the key).
    - both call `mark_restart_needed("models")` and return the same shape as `GET /api/models/catalog`'s `chat` plus `restart_needed`.
- `addons.py`: the `chat` `not_set_up` action stays `open_setup` with short "Needs a chat model"; `images` `not_set_up` action stays `install`. Their messages name "Settings › Chat model" and "Settings › Image models" instead of the current wording that sends owners to the docs (update the `_COPY` entries and their tests).

- [ ] **Step 1: Write the failing tests** (the existing API harness, a fake `Installer` for the job routes, a real one with a mock transport for one end-to-end test):
  - `test_catalog_shape_and_no_paths` — every key above is present; no value contains the data folder path.
  - `test_install_needs_download_step_up` — no token, a `server` token, another session's token: 403 `step_up_required`, `models.install_denied` audited, no job.
  - `test_second_install_while_running_is_busy` — Review Focus 5.
  - `test_install_refuses_unknown_and_unfitting_items`, `test_install_no_space`.
  - `test_urls_never_come_from_the_request` — a body with `"items": ["https://evil.example/x"]` is 400 `unknown_item` and nothing is requested.
  - `test_switch_local_marks_restart` and `test_switch_local_refuses_missing_card`.
  - `test_external_needs_settings_step_up_and_loopback` — a non-loopback URL is 400 `bad_url`; a good one writes `config.toml`, keeps its comments, stores the key, and the key appears in no response, audit detail or log record.
  - `test_cancel_and_retry_need_only_the_session`.
  - In `test_owner_copy.py`: the changed `_COPY` entries pass `forbidden_in`.

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest -q tests/test_api_model_install.py tests/test_api_models_images.py tests/test_owner_copy.py tests/test_addons.py tests/test_api_auth*.py` — PASS.

- [ ] **Step 5: Commit** `feat(api): model catalog, install jobs and chat switching`.

---

### Task 7: Settings › Chat model and Image models

**Files:**
- Create: `web/src/components/DownloadList.tsx`, `web/src/components/ModelsPanel.tsx`
- Modify: `web/src/api/models.ts`, `web/src/api/webauthn.ts` (purpose `download`), `web/src/views/Settings.tsx` (the panel after Add-ons, inside `ServerProvider`), `web/src/shell/AddonsContext.tsx` (`SettingsAnchor` gains `'settings-chat' | 'settings-images'`; `actionTarget`: chat `open_setup` → `settings-chat`, images `install` → `settings-images`), `web/src/components/ModelList.tsx` (its line about card files says "Add models in Settings › Chat model"), `web/src/components/RestartNotice.tsx` (`place` gains `'models'`)
- Test: `web/src/__tests__/models.test.tsx`, plus the existing Settings, add-ons and ModelList tests that pin the changed lines

**Interfaces:**
- Consumes: the Task 6 endpoints; `api`, `post`, `isApiError` (`api/client.ts`); `stepUp`, `passkeyErrorMessage`, `PASSKEY_NOT_CONFIRMED` (`api/webauthn.ts`); `ServerProvider`, `RestartNotice`; `useAddon`, `useOpenSettings`.
- Produces:
  - `models.ts`: `getCatalog(): Promise<Catalog>`, `startInstall(items: string[], makeDefault: boolean): Promise<Job>` (does the `download` step-up itself), `getJob(): Promise<Job | null>`, `cancelInstall()`, `retryInstall()`, `useLocalChat(card: string)`, `useExternalChat(baseUrl: string, apiKey: string | null)` (does the `settings` step-up), and the `Catalog`, `CatalogItem`, `Job`, `JobItem` types matching Task 6.
  - `DownloadList({ job, onRetry, onCancel }: { job: Job; onRetry(): void; onCancel(): void })` — one row per item: name, size (`formatBytes`), progress bar (`role="progressbar"` with `aria-valuenow` in percent, the fill moved with `transform: scaleX()`, not `width`), speed while downloading, a "Verified" tick once checked, and a plain error line with Retry for a failed item. The list has an `aria-live="polite"` summary ("2 of 3 done"). It's presentational only, so the 2c wizard can reuse it.
  - `ModelsPanel` — two sections:
    - **Chat model** (`id="settings-chat"`): what's in use (local model name, or "A model server you run" with its address), installed chat models with "Use this", the catalog's chat models with size, licence link, note and a "Recommended for this PC" badge (with the PC line "NVIDIA GeForce RTX 4080, 16 GB" or "No NVIDIA graphics card found: models run on the processor, more slowly"); "Download" lists exactly what will be fetched and its total size before asking for the passkey; a "Use a model server I already run" form (address, optional key; the key field is `type="password"` and never shown back).
    - **Image models** (`id="settings-images"`): ComfyUI and the two image models with sizes; disabled with "Needs an NVIDIA graphics card" when `pc.nvidia` is false.
    - While a job runs, both sections show the one `DownloadList`, polled every second (stopped when the job ends or the panel unmounts; `visibilitychange` pauses it). After a job or switch: `RestartNotice place="models"` when supervised, else "Restart Hearthkeep to use it."
    - Errors map to plain sentences: `no_space` "There isn't enough free space: about N GB is needed and M GB is free.", `network` "The download stopped. Retry to continue from where it stopped.", `checksum` "The file didn't match its checksum, so it was removed. Retry to download it again.", `unpack` "The download couldn't be unpacked. Retry, or check that antivirus isn't blocking it.", `config_not_updated` "The files are ready, but Hearthkeep couldn't update its model settings file. Retry, or ask for help with the file.", `busy` "A download is already running.", `doesnt_fit` "This model needs more graphics memory than this PC has."

- [ ] **Step 1: Write the failing tests** in `models.test.tsx` (fake `fetch` like the other view tests):
  - `shows the recommendation and PC line`;
  - `download lists the files and total size, asks for a passkey, then shows progress` — `stepUp` called with `"download"`; the POST body holds item ids only;
  - `progress bar uses transform and reports percent` — `aria-valuenow="50"` and `style.transform` contains `scaleX(0.5)`;
  - `failed item shows its sentence and Retry calls retry`;
  - `no_space shows needed and free space`;
  - `switching to an installed model shows the restart notice`;
  - `external server form never shows the key back` and uses `stepUp("settings")`;
  - `image models are disabled without an NVIDIA GPU`;
  - `Set up on the chat card opens settings-chat` (from AddonsPanel / SetUpPanel);
  - `every new string passes forbiddenIn`.

- [ ] **Step 2: Run** `npx vitest run src/__tests__/models.test.tsx` — FAIL. **Step 3: Implement.** **Step 4: Run** `npm test` and `npm run build` — PASS.

- [ ] **Step 5: Commit** `feat(web): choose, download and switch chat and image models in Settings`.

---

### Task 8: Docs and the end-to-end check

**Files:**
- Modify: `docs/install.md` (§4 Chat model, §5 Image models, §6 `models.toml`, §9 external key), `docs/models.md` (cards, catalog, `chat_server`, doing it by hand), `README.md` (quick start line about models; feature list), `server/src/hearthkeep/config.example.toml` (the "copy models.example.toml" line), `models.example.toml` at the repo root (`chat_server` commented example)
- Test: the existing docs and copy tests

- [ ] **Step 1: Rewrite install.md §4–§6 for owners**: "Open Settings › Chat model, choose a model, select Download and confirm with your passkey; restart when asked." with what each recommended model needs (sizes from the catalog), how long a download may take, that it resumes, and the licence links. Keep the manual route (own llama.cpp, own GGUF, hand-written `models.toml`) in `docs/models.md` under "Doing it by hand", for developers. §9 becomes "Settings › Chat model › Use a model server I already run". No CLI command or placeholder in owner sections.

- [ ] **Step 2: Hand check on the Windows dev PC with a throwaway data folder** (never the owner's): run `serve` against a temp data folder and the real catalog, start `chat-qwen3.5-4b` from Settings, pull the network cable (or disable Wi-Fi) mid-way, reconnect and Retry, confirm it resumes and verifies, restart, and send a chat message. Record the result (and whether `-ngl 999` on the CPU build starts cleanly) in the task report. ComfyUI and image downloads (≈ 22 GB) go on the release gate list instead of this step.

- [ ] **Step 3: Run** `uv run pytest -q`, `npm test`, `npm run build`, `python scripts/privacy_scan.py`. Expected: all pass.

- [ ] **Step 4: Commit** `docs: models come from Settings › Chat model`.

## Release gate additions (before v0.1)

- [ ] Install `chat-gemma-4-12b` on an NVIDIA PC with 12 GB or more, and `chat-qwen3.5-9b` on an 8 GB card; chat with each after the restart.
- [ ] Install ComfyUI and both image models; confirm `tar.exe` unpacks the `.7z`, the `python`/`main` paths match, and an image is made with each card.
- [ ] Run `python scripts/pin_catalog.py --check` the day the release is cut.
