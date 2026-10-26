# Models, GPU switching and images

Hearthkeep can run its own models: a chat model through [llama.cpp](https://github.com/ggml-org/llama.cpp) (`llama-server`) and image models through [ComfyUI](https://github.com/comfyanonymous/ComfyUI). Everything runs on your PC and every model server listens on `127.0.0.1` only.

Two kinds of file describe your setup:

- **Model cards** say what a model is and how to run it. Three ship with Hearthkeep.
- **`<data>/models.toml`** says where things are on your machine: the programs and the model folders.

`<data>` means your own Hearthkeep data folder.

## Model cards

A card is a small TOML file. Hearthkeep loads the shipped cards first, then every `*.toml` in `<data>/models/cards/`. A card of yours with the same `id` as a shipped one replaces it. A card with a mistake is skipped, and the server prints what was wrong with it at start-up; the other cards still load.

### The shipped chat card

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

Copy `models.example.toml` to `<data>/models.toml` and fill it in. Values that are empty or start with `<` count as not set.

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

# Chat models: the folder holding each card's files (by card id).
[model_dirs]
"gemma-4-12b" = "<path to the folder with the Gemma GGUF files>"
```

- Chat is managed by Hearthkeep when `llama_server_exe` and a `model_dirs` entry for the chat model are set. Hearthkeep starts `llama-server` when it is first needed and stops it when Hearthkeep stops.
- Images are managed when `comfyui_python`, `comfyui_main` and `comfyui_model_root` are all set. The image file names from the cards are looked up in the `diffusion_models`, `text_encoders` and `vae` folders under `comfyui_model_root`.
- Without `models.toml`, Hearthkeep talks to the chat server already running at `chat_base_url` in `config.toml`, and image generation is off.
- Hearthkeep asks `nvidia-smi` (so an NVIDIA GPU) how much GPU memory you have. If that fails it assumes 16 GB and logs a warning. Set `gpu_vram_gb` whenever it guesses wrong.
- An unknown key or a wrong type in `models.toml` stops the server at start-up with a message naming the key.

## GPU switching

A chat model and an image model usually do not fit in the GPU's memory together, so Hearthkeep swaps them. A small scheduler keeps track of what is loaded and what each model needs (`vram_gb`, plus about 1.5 GB kept free for the system).

- **Chat pauses while images render.** When the assistant makes an image, the chat model is unloaded to make room, the image is rendered, and the chat model is loaded again when something needs it. Anything you type in the meantime waits its turn: an image waits for a chat reply that is already in progress, and a chat waits for an image that is being rendered.
- **The first image after a chat takes longer**, and so does **the first chat after images**, because a model has to be loaded from disk. Later requests of the same kind are quick.
- Several images in one request are rendered one after another. Chat can run between two of them.
- If a model needs more memory than your GPU has, Hearthkeep says so instead of trying.

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

## Troubleshooting

Logs are in `<data>/logs/`: `llama-server.log` for the chat model and `comfyui.log` for the image server. Start there.

- **No images, or the image tool is missing.** Check that all three `comfyui_*` paths are set in `<data>/models.toml` and point to real files.
- **A model "couldn't be started".** Open the matching log. The usual causes are a wrong path, a missing model file, or too little free GPU memory because another program is using it.
- **A card does not show up.** Look at the start-up output: skipped cards are listed with the reason. Common ones are a file name that contains a path, a missing `files` entry, or a `workflow` name that is not one of the shipped files.
- **"Hearthkeep's data folder path is too long for llama-server on Windows".** On Windows, `<data>\runtime\llama-api-key` (and the model file paths) must each stay under 260 characters. Move the data folder to a shorter path.
- **The first image is slow.** That is the model swap described above.
- **Port already in use.** Change `llama_port` or `comfyui_port` in `models.toml`.
