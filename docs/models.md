# Models, GPU switching and images

Hearthkeep can run its own models: a chat model through [llama.cpp](https://github.com/ggml-org/llama.cpp) (`llama-server`) and image models through [ComfyUI](https://github.com/comfyanonymous/ComfyUI). Everything runs on your PC and every model server listens on `127.0.0.1` only.

Three things describe your setup:

- **The catalog** is the list of programs and model files Hearthkeep can download for you from Settings › Chat model and Settings › Image models.
- **Model cards** say what a model is and how to run it. Five ship with Hearthkeep: three chat models (Qwen3.5 4B, Qwen3.5 9B and Gemma 4 12B) and two image models (Z-Image Turbo and FLUX.2 klein 4B).
- **`<data>/models.toml`** says where things are on your machine: the programs and the model folders. Hearthkeep writes it for you when you download or switch a model; you can still write it by hand.

`<data>` means your own Hearthkeep data folder.

To download models the easy way, see [install.md](install.md#4-chat-model). To set everything up by hand, see [Doing it by hand](#doing-it-by-hand).

## The model catalog

The catalog ships with Hearthkeep as `server/src/hearthkeep/models/catalog.toml`. Each entry names its files with an exact download address, size and SHA-256, its licence and a link to it. Nothing else can be downloaded: a request names a catalog entry, never an address.

| Entry | Size | Needs | Licence |
|---|---|---|---|
| llama.cpp b12099, CUDA build (with its CUDA runtime) | 0.7 GB | An NVIDIA graphics card | MIT |
| llama.cpp b12099, Vulkan build | 38 MB | Another graphics card (an alternative, never recommended) | MIT |
| llama.cpp b12099, CPU build | 25 MB | Nothing | MIT |
| Qwen3.5 4B (`qwen3.5-4b`, Q4_K_M) | 3.0 GB | The processor alone, or under 6 GB of graphics memory | Apache 2.0 |
| Qwen3.5 9B (`qwen3.5-9b`, Q4_K_M) | 6.2 GB | About 8 GB of graphics memory | Apache 2.0 |
| Gemma 4 12B (`gemma-4-12b`, Q6_K, with its vision file) | 10.4 GB | 11 GB of graphics memory or more | Apache 2.0 |
| ComfyUI portable v0.44.0 (NVIDIA, CUDA 12.6) | 1.9 GB | An NVIDIA graphics card | GPL-3.0 |
| Z-Image Turbo | 20.7 GB | An NVIDIA graphics card | Apache 2.0 |
| FLUX.2 klein 4B | 16.1 GB | An NVIDIA graphics card | Apache 2.0 |

The two image models share one text file (`qwen_3_4b`), which is downloaded once. The licence links are in `catalog.toml`.

**The recommendation.** The largest chat model whose card `vram_gb` fits the graphics memory found (`gpu_vram_gb` from `models.toml` when set, else what `nvidia-smi` reports). With no NVIDIA graphics card it is Qwen3.5 4B on the CPU build. The engine is the CUDA build when `nvidia-smi` answers, otherwise the CPU build; the Vulkan build is offered as an alternative because it can't be detected reliably.

**Where the files go.**

| What | Folder |
|---|---|
| Chat engine | `<data>/tools/llama.cpp/<build>` (for example `b12099-cuda`) |
| ComfyUI | `<data>/tools/comfyui` |
| Chat models | `<data>/models/chat/<card id>`, under the file names in the card |
| Image models | `<data>/models/image/diffusion_models`, `text_encoders` and `vae` |
| Unfinished downloads | `<data>/runtime/downloads` (a `.part` file per download) |

If `comfyui_model_root` in `models.toml` already points at the owner's own ComfyUI models folder, image files go there instead. A model whose files are already in place (for example one you installed by hand) shows as installed and is never downloaded again.

**How a download works.**

- Starting one needs a passkey check (step-up purpose `download`). Cancelling, retrying and switching between installed models need only the session. Every one is written to the audit log.
- One job runs at a time, held in memory. A restart of the server forgets the job but keeps the `.part` files, so starting the same items again resumes them with an HTTP range request.
- Every hop of every download, redirects included, must be HTTPS on port 443, with no user name, to `github.com`, `huggingface.co`, or a host ending in `.githubusercontent.com` or `.hf.co`. Every Hugging Face address pins a commit, never `main`.
- Each file is checked against its SHA-256 before use. A mismatch deletes the file and fails the item at once. A network failure is retried up to three times by itself.
- Archives are unpacked into a staging folder first (zip by Hearthkeep, the ComfyUI `.7z` by Windows' own `tar.exe`), checked for links, then moved into place.
- Free space is checked first; a download that would not fit is not started.

**What changes in `models.toml`.** After a successful job, or a switch in Settings, Hearthkeep edits only the keys it manages: `llama_server_exe`, `[model_dirs]`, `default_chat_model`, `chat_server`, `comfyui_python`, `comfyui_main`, `comfyui_model_root` and `default_image_model`. Every other line and comment stays as it was, and the first time it edits the file it saves a copy as `models.toml.before-hearthkeep`. If the file can't be edited safely (for example a managed key written as an inline table), the edit is refused and the file is left alone; the item then shows that its settings file wasn't updated. It then asks for a restart (`ServerControl.mark_restart_needed("models")`); the restart notice offers **Restart now** when Hearthkeep runs under its launcher, and otherwise says "Restart Hearthkeep to use it."

**Secrets.** The key of the llama-server Hearthkeep runs is its own secret, `llama_api_key`, made automatically. The key of an external chat server is a separate secret, `chat_api_key` (the name in `chat_api_key_secret` in `config.toml`). Neither is ever written to a settings file, a log or the audit log.

**The pin script.** When the catalog changes, `python scripts/pin_catalog.py` fills in and checks sizes and SHA-256 values from the GitHub releases API and the Hugging Face tree API without downloading the files; `--check` only reports. It runs by hand and never in CI, and should be run the day a release is cut.

## Model cards

A card is a small TOML file. The catalog's chat and image entries each name the card they install. Hearthkeep loads the shipped cards first, then every `*.toml` in `<data>/models/cards/`. A card of yours with the same `id` as a shipped one replaces it. A card with a mistake is skipped, and the server prints what was wrong with it at start-up; the other cards still load.

### The shipped chat cards

Gemma 4 12B, the card every other chat card is modelled on:

```toml
id = "gemma-4-12b"
name = "Gemma 4 12B"
kind = "chat"
provider = "llama_server"
capabilities = ["vision", "tools", "thinking"]
vram_gb = 11.0
context = 32768
launch_args = [
    "--model", "{model}",
    "--mmproj", "{mmproj}",
    "--alias", "gemma-4-12b",
    "--host", "127.0.0.1",
    "--port", "{port}",
    "--api-key-file", "{api_key_file}",
    "-c", "32768",
    "-ngl", "999",
    "--jinja",
]

[files]
model = "gemma-4-12b-it-Q6_K.gguf"
mmproj = "mmproj-F16.gguf"

[sampling]
temperature = 1.0
top_k = 64
top_p = 0.95
min_p = 0.0
```

The Qwen3.5 cards (`qwen3.5-4b` with `vram_gb = 4.0` and a 16384-token context, `qwen3.5-9b` for about 8 GB) are the same shape without the vision file. A downloaded Gemma 4 12B is saved under this card's two file names, so a copy you installed by hand keeps working.

### A short image card

```toml
id = "z-image-turbo"
name = "Z-Image Turbo"
kind = "image"
provider = "comfyui"
capabilities = ["text-in-image", "photoreal"]
vram_gb = 10.5
default_steps = 8
workflow = "z-image-turbo.json"
prefer_when = "the image contains words, or a photoreal scene"
guidance = """
Write plain, descriptive English. Describe the subject, the setting, the light, and the lens or style.
Put any exact text that must appear in the image in double quotes.
"""

[files]
unet = "z_image_turbo_bf16.safetensors"
text_encoder = "qwen_3_4b.safetensors"
vae = "ae.safetensors"
```

### Every field

| Field | Needed | What it is |
|---|---|---|
| `id` | always | Lower-case letters, digits, `.` and `-`, up to 64 characters, starting with a letter or digit. |
| `name` | always | The name shown in the app. |
| `kind` | always | `chat` or `image`. |
| `provider` | no | `llama_server` for chat cards, `comfyui` for image cards. Filled in from `kind` if you leave it out; any other value is an error. |
| `vram_gb` | always | How much GPU memory the model needs, between 0.5 and 80. The scheduler uses it to decide what fits (see GPU switching). |
| `capabilities` | no | A list of short labels. They are shown in the models list. The label `image-edit` has a meaning: see "Editing photos". |
| `files` | always | The file names (never paths) of the model's files. Chat cards: `model` (required) and `mmproj` (optional, for vision). Image cards: `unet`, `text_encoder` and `vae`, all three. |
| `launch_args` | chat only | The arguments passed to `llama-server`. See below. Image cards must not have it. |
| `context` | chat only | The context size in tokens. Must be at least 1 for a chat card. |
| `sampling` | no | A table of sampling settings (temperature, `top_k`, `top_p`, `min_p`). |
| `workflow` | image only | The ComfyUI workflow used to make images. A file name ending in `.json`. |
| `edit_workflow` | no | A second workflow used to edit a photo. Only used for a card that also lists `image-edit`. |
| `default_steps` | image only | How many sampling steps to run. |
| `guidance` | no | Text that teaches the chat model how to write a prompt for this model. It becomes part of the image tool's description. |
| `prefer_when` | no | A short phrase saying when the chat model should pick this model (for example "the image contains words"). |

**`launch_args` placeholders.** Only four are filled in: `{model}`, `{mmproj}`, `{port}` and `{api_key_file}`. `{model}` and `{mmproj}` become the matching `files` entry inside the folder you list in `models.toml`; `{port}` is `llama_port`; `{api_key_file}` is a key file Hearthkeep generates for the run (on Windows its path must stay under 260 characters, see Troubleshooting). `--port` and `--api-key-file` must use `{port}` and `{api_key_file}` (a different value is refused), and `--api-key` is not allowed: Hearthkeep supplies the key itself. These rules are checked when the model starts, not when the card is loaded. Any other braces are passed through untouched, so JSON values such as `--chat-template-kwargs` work as written. If you use `{model}` or `{mmproj}`, the card must list that file. Hearthkeep refuses a card that tries to listen on anything other than `127.0.0.1`: `--host` must be followed by `127.0.0.1`, and `0.0.0.0` or `::` is rejected anywhere in the arguments.

### Adding your own card

1. Create the folder `<data>/models/cards/` and put a `.toml` file in it. Copy a shipped card as a starting point.
2. Put the model files where `models.toml` expects them (chat models in the folder named under `[model_dirs]`; image models in the ComfyUI folders, see below).
3. Restart Hearthkeep. The start-up output lists the cards it loaded and any it skipped, with the reason.

**Known Phase 2 limit.** An image card in `<data>/models/cards/` can only point `workflow` and `edit_workflow` at the workflow files that ship with Hearthkeep (`z-image-turbo.json`, `flux2-klein-4b.json` and `flux2-klein-4b-edit.json`). It cannot bring its own workflow graph yet. In practice your own image card is useful for different weight files, a different name, guidance or step count on one of those graphs, not for a different kind of model.

### Editing photos: `image-edit` and `edit_workflow`

To let a model edit a photo you uploaded, an image card needs both:

- `"image-edit"` in `capabilities`, and
- an `edit_workflow`.

The shipped FLUX.2 klein card has both:

```toml
capabilities = ["photoreal", "image-edit"]
workflow = "flux2-klein-4b.json"
edit_workflow = "flux2-klein-4b-edit.json"
```

Without both, the model can still make images from text but is never used for editing. If you ask for an edit and no card can do it, the assistant says that none of the image models can edit photos.

## `models.toml`

Hearthkeep reads `<data>/models.toml` when it starts, exactly as before the catalog existed: a hand-written file with your own paths, `-ngl 999` cards or `chat_base_url` keeps working. Values that are empty or start with `<` count as not set. You can leave the file out and use only Settings, or write it yourself (see [Doing it by hand](#doing-it-by-hand)), or both: Hearthkeep edits only the keys it manages (see [the catalog](#the-model-catalog)).

```toml
# Chat server (llama.cpp)
llama_server_exe = "<path to llama-server executable>"
llama_port = 8080

# Image server (ComfyUI)
comfyui_python = "<path to the Python used by ComfyUI>"
comfyui_main = "<path to ComfyUI main.py>"
comfyui_port = 8188
# A folder containing diffusion_models/, text_encoders/, vae/ and loras/.
comfyui_model_root = "<path to the ComfyUI models folder>"

# Override the detected GPU memory, in GB (optional).
# gpu_vram_gb = 16

default_chat_model = "gemma-4-12b"
default_image_model = "z-image-turbo"

# "local" runs the chat model Hearthkeep manages; "external" uses chat_base_url from config.toml.
# chat_server = "local"

# Chat models: the folder holding each card's files (by card id).
[model_dirs]
"gemma-4-12b" = "<path to the folder with the Gemma GGUF files>"
```

| Key | What it does |
|---|---|
| `llama_server_exe` | Path to `llama-server` (`llama-server.exe` on Windows). With this and a `model_dirs` entry for the default chat model, Hearthkeep starts and stops llama-server itself. |
| `llama_port` | The port llama-server listens on, on `127.0.0.1`. Default 8080. |
| `comfyui_python` | The Python that runs ComfyUI. In the portable build, `python_embeded\python.exe`. |
| `comfyui_main` | ComfyUI's `main.py`. |
| `comfyui_port` | The port ComfyUI listens on, on `127.0.0.1`. Default 8188. |
| `comfyui_model_root` | A folder with `diffusion_models`, `text_encoders` and `vae` inside. The image cards' file names are looked up there. Images are on only when all three `comfyui_*` paths are set. |
| `gpu_vram_gb` | Optional. Your GPU memory in GB. Hearthkeep reads it from `nvidia-smi`; if that fails it assumes 16 GB and logs a warning. Set it when the guess is wrong. The catalog's recommendation uses it too. |
| `default_chat_model` | The card id of the chat model to use. Default `gemma-4-12b`. |
| `default_image_model` | The card id of the image model to use when you don't ask for one. Default `z-image-turbo`. |
| `chat_server` | `"local"` or `"external"`. Unset means the old behaviour: Hearthkeep runs llama-server whenever `llama_server_exe` and the `model_dirs` entry are both set, and uses `chat_base_url` otherwise. `"external"` makes Hearthkeep use the server at `chat_base_url` even though local models are installed, so switching back and forth in Settings never deletes anything. It has no effect unless `config.toml` sets `chat_base_url`. |
| `[model_dirs]` | For each chat card id, the folder that holds its files. |

- Chat is managed by Hearthkeep when `llama_server_exe` and a `model_dirs` entry for the chat model are set (and `chat_server` is not `"external"`). Hearthkeep starts `llama-server` when it is first needed and stops it when Hearthkeep stops.
- Images are managed when `comfyui_python`, `comfyui_main` and `comfyui_model_root` are all set. The image file names from the cards are looked up in the `diffusion_models`, `text_encoders` and `vae` folders under `comfyui_model_root`.
- Without `models.toml`, image generation is off. Chat uses the server at `chat_base_url` when `config.toml` sets it. With neither `models.toml` nor `chat_base_url`, Hearthkeep starts without a chat model and Settings › Add-ons says so; download one in Settings › Chat model.
- Hearthkeep asks `nvidia-smi` (so an NVIDIA GPU) how much GPU memory you have. If that fails it assumes 16 GB and logs a warning. Set `gpu_vram_gb` whenever it guesses wrong.
- An unknown key or a wrong type in `models.toml` stops the server at start-up with a message naming the key.
- Hearthkeep's own edits change only top-level `key = value` lines (basic strings, numbers and booleans) and `[model_dirs]` entries, append what is missing, then re-read the result and check the values. If your file uses something else for a managed key, the edit is refused and the file is untouched.
- `models.example.toml` in the repository is a template with placeholders and a commented `chat_server` line.

## Doing it by hand

For developers, and for anyone who already runs their own llama.cpp, model files or ComfyUI. None of this is needed if you use Settings. Placeholders: `<tools>` is a folder of your choice for programs and model files, for example `C:\AI`; `<data>` is your Hearthkeep data folder.

### Your own llama.cpp

1. Open the llama.cpp releases page: [github.com/ggml-org/llama.cpp/releases](https://github.com/ggml-org/llama.cpp/releases).
2. For Windows with an NVIDIA GPU, download the CUDA build, `llama-<build>-bin-win-cuda-<version>-x64.zip`, and the matching CUDA runtime, `cudart-llama-bin-win-cuda-<version>-x64.zip`. Pick the CUDA version your driver supports (`nvidia-smi` shows it top right). Other graphics cards can use the Vulkan build, and any PC the CPU build.
3. Extract both zips into the same folder, for example `<tools>\llama.cpp`.
4. Check that it runs:

   ```
   <tools>\llama.cpp\llama-server.exe --version
   ```

**macOS:** use the `macos-arm64` release or `brew install llama.cpp`. **Linux:** build from source with CUDA as the llama.cpp README describes. Both are untested as a Hearthkeep host.

### Your own chat model files

The shipped Gemma card expects two files, with exactly these names, in one folder:

| File | What it is | Size (approx.) |
|---|---|---|
| `gemma-4-12b-it-Q6_K.gguf` | Gemma 4 12B instruct, Q6_K quantisation | 10.2 GB |
| `mmproj-F16.gguf` | The vision projector, so the model can see images | 0.12 GB |

1. Find a GGUF build of Gemma 4 12B instruct on [Hugging Face](https://huggingface.co/models?search=gemma-4-12b%20gguf) that offers a `Q6_K` file and an F16 `mmproj` file. Gemma's official page is [ai.google.dev/gemma](https://ai.google.dev/gemma).
2. Put both in one folder, for example `<tools>\models\gemma-4-12b`. Rename a file whose name differs, or write your own card ([Adding your own card](#adding-your-own-card)).
3. Where a SHA-256 is published, compare it: `Get-FileHash <file> -Algorithm SHA256` (on macOS and Linux, `shasum -a 256 <file>`).

The Qwen3.5 cards expect `Qwen_Qwen3.5-4B-Q4_K_M.gguf` and the matching 9B file in their own folders, the same way. Gemma 4 12B needs about 11 GB of GPU memory with a 32K context; the other cards say how much in their `vram_gb`.

### Your own ComfyUI and image files

1. Get ComfyUI: [github.com/comfyanonymous/ComfyUI](https://github.com/comfyanonymous/ComfyUI). Its README links the Windows portable build for NVIDIA GPUs on the releases page. It must be new enough for FLUX.2 klein and Z-Image (the shipped workflows use nodes such as `Flux2Scheduler` and `EmptyFlux2LatentImage`). Extract the `.7z` with [7-Zip](https://www.7-zip.org/) into `<tools>`.
2. The three paths `models.toml` needs, in the portable build:

   | `models.toml` key | Path |
   |---|---|
   | `comfyui_python` | `<tools>\ComfyUI_windows_portable\python_embeded\python.exe` |
   | `comfyui_main` | `<tools>\ComfyUI_windows_portable\ComfyUI\main.py` |
   | `comfyui_model_root` | `<tools>\ComfyUI_windows_portable\ComfyUI\models` |

3. The shipped cards expect these files, with exactly these names, under `comfyui_model_root`:

   | File | Folder | Used by |
   |---|---|---|
   | `z_image_turbo_bf16.safetensors` | `diffusion_models` | Z-Image Turbo |
   | `flux-2-klein-4b.safetensors` | `diffusion_models` | FLUX.2 klein 4B |
   | `qwen_3_4b.safetensors` | `text_encoders` | both (shared) |
   | `ae.safetensors` | `vae` | Z-Image Turbo |
   | `flux2-vae.safetensors` | `vae` | FLUX.2 klein 4B |

   The catalog downloads Comfy-Org's repackaged files from Hugging Face. The model makers' pages are [Tongyi-MAI](https://huggingface.co/Tongyi-MAI) (Z-Image) and [Black Forest Labs](https://huggingface.co/black-forest-labs) (FLUX.2 klein).

You don't need to start ComfyUI yourself. Hearthkeep starts it on `127.0.0.1` when the first image is needed. You can install just one image model; if it is FLUX.2 klein, set `default_image_model = "flux2-klein-4b"`.

### Write `models.toml`

Copy `models.example.toml` to `<data>/models.toml` and fill it in. Use single quotes on Windows so backslashes are taken literally:

```toml
llama_server_exe = '<tools>\llama.cpp\llama-server.exe'
llama_port = 8080

comfyui_python = '<tools>\ComfyUI_windows_portable\python_embeded\python.exe'
comfyui_main = '<tools>\ComfyUI_windows_portable\ComfyUI\main.py'
comfyui_port = 8188
comfyui_model_root = '<tools>\ComfyUI_windows_portable\ComfyUI\models'

default_chat_model = "gemma-4-12b"
default_image_model = "z-image-turbo"

[model_dirs]
"gemma-4-12b" = '<tools>\models\gemma-4-12b'
```

Restart Hearthkeep. Settings › Chat model then shows the model you set up as installed.

### Your own chat server

To use a chat server you already run, either use **Use a model server I already run** in Settings › Chat model, or set `chat_base_url` in `config.toml` (a `127.0.0.1`, `::1` or `localhost` address, for example `http://127.0.0.1:8080/v1`) and, if the server needs a key, store it as the secret named in `chat_api_key_secret`:

```
uv run hearthkeep secrets set chat_api_key --data <data>
uv run hearthkeep secrets list --data <data>
```

The value is read from a hidden prompt and `secrets list` prints names only. Add `chat_server = "external"` to `models.toml` if local models are also installed and you want the server to win.

## GPU switching

A chat model and an image model usually do not fit in the GPU's memory together, so Hearthkeep swaps them. A small scheduler keeps track of what is loaded and what each model needs (`vram_gb`, plus about 1.5 GB kept free for the system).

- **Chat pauses while images render.** When the assistant makes an image, the chat model is unloaded to make room, the image is rendered, and the chat model is loaded again when something needs it. Anything you type in the meantime waits its turn: an image waits for a chat reply that is already in progress, and a chat waits for an image that is being rendered.
- **The first image after a chat takes longer**, and so does **the first chat after images**, because a model has to be loaded from disk. Later requests of the same kind are quick.
- Several images in one request are rendered one after another. Chat can run between two of them.
- If ComfyUI doesn't give its GPU memory back within about 5 seconds (it is stuck, or a render is still finishing), Hearthkeep stops ComfyUI so the chat model can start. ComfyUI starts again with the next image.
- If a model needs more memory than your GPU has, Hearthkeep says so instead of trying.

## Scanned pages and the `vision` capability

When a PDF page is a scan (a picture of text, not text), the assistant can read it with the chat model's vision. The page is drawn inside the sandbox and shown to the chat model, up to 10 pages at a time, and the result is saved as a run like any other. It needs a chat model that can look at images: a card that lists the `vision` capability and an `mmproj` file under `files`. The shipped Gemma 4 12B card has both. Numbers read this way are labelled `read from page N, not computed`, because the model read them rather than a script computing them. Reading scans needs Docker, like the rest of the file tools (see [install.md](install.md#11-python-sandbox-docker)).

## Photo uploads

You can attach a photo to a message and ask for an edit, for example "make the apple green". The photo is stored on your PC, and the assistant can only use it by its id (the app adds an `Attached photo:` line to your message).

Limits, checked when you upload:

- PNG, JPEG or WebP only. The type is read from the file's contents, not from its name.
- Up to 15 MB.
- Up to 4096 pixels on each side.
- Still images only. Animated files are rejected.

Generated images use the shapes `square` (1024x1024), `landscape` (1536x864), `portrait` (864x1536) and `banner` (1536x640), or a custom width and height (multiples of 16, between 512 and 2048). When editing a photo and no shape is asked for, Hearthkeep uses the shape nearest to the photo's own.

Known Phase 2 limits:

- Uploaded photos never expire. They stay in `<data>` until you delete them yourself.
- To edit a photo, Hearthkeep sends a copy to ComfyUI, which keeps that copy in its own `input` folder. Hearthkeep does not clean that folder; delete the files there if you want them gone.
- ComfyUI also keeps a copy of every generated image in its own `output` folder (files whose names start with `hearthkeep`), next to the copy in `<data>`. Hearthkeep does not clean that folder either.

## Troubleshooting

Logs are in `<data>/logs/`: `llama-server.log` for the chat model and `comfyui.log` for the image server. Start there.

For problems outside the models (ports, enrollment codes, passkeys, start-up errors), see [Troubleshooting in install.md](install.md#16-troubleshooting).

- **No images, or the image tool is missing.** Download ComfyUI and an image model in Settings › Image models (an NVIDIA graphics card is needed), or, if you set it up by hand, check that all three `comfyui_*` paths are set in `<data>/models.toml` and point to real files.
- **A download failed.** The reason is shown in Settings. A network error resumes from the `.part` file with **Retry**; a checksum error removes the file; "the settings file wasn't updated" means `models.toml` could not be edited safely (for example it isn't valid TOML), so fix it or move it aside, then retry. The first edit saved `models.toml.before-hearthkeep` next to it.
- **llama.cpp's CPU build and `-ngl 999`.** The shipped cards pass `-ngl 999`. On the CPU build (llama.cpp b12099) llama-server starts cleanly with it and runs on the processor, as checked with Qwen3.5 4B.
- **A model "couldn't be started".** Open the matching log. The usual causes are a wrong path, a missing model file, or too little free GPU memory because another program is using it.
- **A card does not show up.** Look at the start-up output: skipped cards are listed with the reason. Common ones are a file name that contains a path, a missing `files` entry, or a `workflow` name that is not one of the shipped files.
- **"Hearthkeep's data folder path is too long for llama-server on Windows".** On Windows, `<data>\runtime\llama-api-key` (and the model file paths) must each stay under 260 characters. Move the data folder to a shorter path. The limit applies to llama-server only: ComfyUI (Python) handles long paths.
- **The first image is slow.** That is the model swap described above.
- **Port already in use.** Change `llama_port` or `comfyui_port` in `models.toml`.
