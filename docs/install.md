# Installing Hearthkeep

This guide takes you from a fresh PC to Hearthkeep running with its own models, reachable from your phone or Mac. Windows 11 with an NVIDIA GPU is the tested host. Notes for macOS and Linux are given where they differ; as a host, both are untested.

Placeholders used below:

- `<repo>`: the folder you cloned Hearthkeep into.
- `<data>`: your Hearthkeep data folder (step 3).
- `<tools>`: a folder for llama.cpp, ComfyUI and model files, for example `C:\AI`. Any folder works.
- `<machine>.<tailnet>.ts.net`: your PC's Tailscale name (step 7).

Commands are for PowerShell on Windows unless marked otherwise. Run each command on its own line.

Contents:

1. [Install the prerequisites](#1-install-the-prerequisites)
2. [Clone and build](#2-clone-and-build)
3. [Create a data folder](#3-create-a-data-folder)
4. [Chat model: llama.cpp and Gemma 4 12B](#4-chat-model-llamacpp-and-gemma-4-12b)
5. [Image models (optional): ComfyUI, Z-Image Turbo and FLUX.2 klein](#5-image-models-optional-comfyui-z-image-turbo-and-flux2-klein)
6. [Write `models.toml`](#6-write-modelstoml)
7. [Decide on remote access before your first passkey](#7-decide-on-remote-access-before-your-first-passkey)
8. [First start and your passkey](#8-first-start-and-your-passkey)
9. [Optional: an API key for an external chat server](#9-optional-an-api-key-for-an-external-chat-server)
10. [Run at login (Windows)](#10-run-at-login-windows)
11. [Python sandbox (Docker)](#11-python-sandbox-docker)
12. [Notifications on your phone](#12-notifications-on-your-phone)
13. [Updating](#13-updating)
14. [Backing up](#14-backing-up)
15. [Troubleshooting](#15-troubleshooting)
16. [Uninstalling](#16-uninstalling)

## 1. Install the prerequisites

You need:

| Tool | Version | Where to get it |
|---|---|---|
| Git | any recent | [git-scm.com](https://git-scm.com/downloads) |
| Python | 3.12 or newer | [python.org](https://www.python.org/downloads/) (uv can also install it for you) |
| uv | any recent | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |
| Node.js | 20.19+ or 22.12+ | [nodejs.org](https://nodejs.org/) (the LTS release is fine) |
| NVIDIA driver | recent, with `nvidia-smi` | [nvidia.com/drivers](https://www.nvidia.com/Download/index.aspx) |
| Docker Desktop (optional) | recent | [docker.com](https://www.docker.com/products/docker-desktop/); needed only for Python analysis (step 11) |

1. Install each tool from the links above. On Windows, `winget` works too:

   ```
   winget install --id Git.Git -e
   winget install --id astral-sh.uv -e
   winget install --id OpenJS.NodeJS.LTS -e
   ```

2. Open a **new** terminal so the tools are on your `PATH`.
3. Check the versions:

   ```
   git --version
   uv --version
   node --version
   nvidia-smi
   ```

   `node --version` must print 20.19 or later (or 22.12 or later). `nvidia-smi` must list your GPU; Hearthkeep uses it to read how much GPU memory you have.

**macOS and Linux:** install the same tools with your package manager (for example Homebrew on macOS). `nvidia-smi` only exists with an NVIDIA GPU; without it, see `gpu_vram_gb` in step 6.

## 2. Clone and build

1. Clone the repository:

   ```
   git clone <repository URL> <repo>
   ```

2. Build the web app. The server serves the built files from `web/dist`.

   ```
   cd <repo>\web
   npm ci
   npm run build
   ```

3. Install the server and its dependencies. uv creates a virtual environment in `<repo>\server\.venv`.

   ```
   cd <repo>\server
   uv sync
   ```

4. Check that the command works:

   ```
   uv run hearthkeep --help
   ```

   It lists three commands: `serve`, `enroll-code` and `secrets`.

## 3. Create a data folder

The data folder holds your chats, passkeys, audit log, encrypted secrets, images and logs. It must be **outside** the repository, so nothing personal can ever be committed.

1. Pick a short path. On Windows, for example `C:\Hearthkeep\data`.
2. Create it:

   ```
   mkdir C:\Hearthkeep\data
   ```

3. Use that path wherever this guide says `<data>`.

Keep the path short. llama-server on Windows cannot open a file whose full path is 260 characters or longer. Hearthkeep writes the chat model's key file to `<data>\runtime\llama-api-key` and checks this path (and the model file paths) before it starts llama-server. If any is too long it refuses with "Hearthkeep's data folder path is too long for llama-server on Windows". ComfyUI is not affected.

Choose the location once. The key that encrypts your secrets is stored in the OS keyring under a name derived from the data folder's full path. If you move the folder later, see [Backing up](#14-backing-up).

## 4. Chat model: llama.cpp and Gemma 4 12B

Skip this step if you already run an OpenAI-compatible chat server on this PC and want to keep using it. Leave out `models.toml` (step 6), and Hearthkeep uses the server at `chat_base_url` in `<data>\config.toml` (by default `http://127.0.0.1:8080/v1`). It must be on `127.0.0.1`, `::1` or `localhost`.

### 4.1 llama.cpp

1. Open the llama.cpp releases page: [github.com/ggml-org/llama.cpp/releases](https://github.com/ggml-org/llama.cpp/releases).
2. For Windows with an NVIDIA GPU, download the CUDA build, `llama-<build>-bin-win-cuda-<version>-x64.zip`, and the matching CUDA runtime, `cudart-llama-bin-win-cuda-<version>-x64.zip`. Pick the CUDA version your driver supports (`nvidia-smi` shows it top right).
3. Extract both zips into the same folder, for example `<tools>\llama.cpp`.
4. Check that it runs:

   ```
   <tools>\llama.cpp\llama-server.exe --version
   ```

**macOS:** use the `macos-arm64` release or `brew install llama.cpp`. **Linux:** build from source with CUDA as the llama.cpp README describes. Both are untested as a Hearthkeep host.

### 4.2 Gemma 4 12B weights

The shipped card (`server/src/hearthkeep/models/cards/gemma-4-12b.toml`) expects two files, with exactly these names, in one folder:

| File | What it is | Size (approx.) |
|---|---|---|
| `gemma-4-12b-it-Q6_K.gguf` | Gemma 4 12B instruct, Q6_K quantisation | 9.8 GB |
| `mmproj-F16.gguf` | The vision projector, so the model can see images | about 1 GB |

1. Find a GGUF build of Gemma 4 12B instruct on [Hugging Face](https://huggingface.co/models?search=gemma-4-12b%20gguf) that offers a `Q6_K` file and an F16 `mmproj` file. Gemma's official page is [ai.google.dev/gemma](https://ai.google.dev/gemma); accept the Gemma licence terms where the download asks.
2. Create a folder for them, for example `<tools>\models\gemma-4-12b`.
3. Download both files into it. If a file has a different name, rename it to the name in the table, or write your own card ([docs/models.md](models.md#adding-your-own-card)).
4. Where the repository publishes a SHA-256 for a file, compare it after downloading:

   ```
   Get-FileHash <tools>\models\gemma-4-12b\gemma-4-12b-it-Q6_K.gguf -Algorithm SHA256
   ```

   On macOS and Linux use `shasum -a 256 <file>`.

The model needs about 11 GB of GPU memory with a 32K context.

## 5. Image models (optional): ComfyUI, Z-Image Turbo and FLUX.2 klein

Skip this step if you only want chat. Image generation stays off until the three `comfyui_*` paths are set in `models.toml`.

### 5.1 ComfyUI portable

1. Open the ComfyUI project page: [github.com/comfyanonymous/ComfyUI](https://github.com/comfyanonymous/ComfyUI). Its README links the Windows portable build for NVIDIA GPUs, published on the releases page.
2. Download a recent release. It must be new enough to support FLUX.2 klein and Z-Image (the shipped workflows use nodes such as `Flux2Scheduler` and `EmptyFlux2LatentImage`).
3. Extract it (it is a `.7z` archive; use [7-Zip](https://www.7-zip.org/)) into `<tools>`, for example `<tools>\ComfyUI_windows_portable`.
4. Note three paths for step 6:

   | `models.toml` key | Path in the portable build |
   |---|---|
   | `comfyui_python` | `<tools>\ComfyUI_windows_portable\python_embeded\python.exe` |
   | `comfyui_main` | `<tools>\ComfyUI_windows_portable\ComfyUI\main.py` |
   | `comfyui_model_root` | `<tools>\ComfyUI_windows_portable\ComfyUI\models` |

You don't need to start ComfyUI yourself. Hearthkeep starts it on `127.0.0.1` when the first image is needed and points it at `comfyui_model_root`.

**macOS and Linux:** there is no portable build. Install ComfyUI manually as its README describes and use that install's Python and `main.py`. Untested as a Hearthkeep host.

### 5.2 Image weights

The shipped cards expect these files, with exactly these names, under `comfyui_model_root`:

| File | Folder | Used by | Size (approx.) |
|---|---|---|---|
| `z_image_turbo_bf16.safetensors` | `diffusion_models` | Z-Image Turbo | 12 GB |
| `flux-2-klein-4b.safetensors` | `diffusion_models` | FLUX.2 klein 4B | 8 GB |
| `qwen_3_4b.safetensors` | `text_encoders` | both (shared) | 8 GB |
| `ae.safetensors` | `vae` | Z-Image Turbo | 0.3 GB |
| `flux2-vae.safetensors` | `vae` | FLUX.2 klein 4B | 0.3 GB |

So the layout is:

```
<comfyui_model_root>
├─ diffusion_models\
│  ├─ z_image_turbo_bf16.safetensors
│  └─ flux-2-klein-4b.safetensors
├─ text_encoders\
│  └─ qwen_3_4b.safetensors
└─ vae\
   ├─ ae.safetensors
   └─ flux2-vae.safetensors
```

1. Find the ComfyUI-ready files on Hugging Face. The model makers' pages are [Tongyi-MAI on Hugging Face](https://huggingface.co/Tongyi-MAI) (Z-Image) and [Black Forest Labs on Hugging Face](https://huggingface.co/black-forest-labs) (FLUX.2 klein). ComfyUI's own documentation at [docs.comfy.org](https://docs.comfy.org) lists the files for its example workflows of both models. Accept each model's licence terms where the download asks.
2. Download each file into the folder shown in the table. Create the folders if they don't exist.
3. Rename any file whose name differs from the table, or write your own card ([docs/models.md](models.md#adding-your-own-card)).
4. Check SHA-256 hashes where they are published, as in step 4.2.

Each image model needs about 10 to 10.5 GB of GPU memory. Hearthkeep unloads the chat model while an image renders (see [GPU switching](models.md#gpu-switching)).

You can install just one of the two image models. If you only install FLUX.2 klein, set `default_image_model = "flux2-klein-4b"` in step 6.

## 6. Write `models.toml`

`models.toml` tells Hearthkeep where the programs and model files are. Without it, Hearthkeep uses the chat server at `chat_base_url` and image generation is off.

1. Copy the example into your data folder:

   ```
   copy <repo>\models.example.toml <data>\models.toml
   ```

2. Open `<data>\models.toml` in a text editor and fill in your paths. Use single quotes on Windows so backslashes are taken literally:

   ```toml
   # Chat server (llama.cpp)
   llama_server_exe = '<tools>\llama.cpp\llama-server.exe'
   llama_port = 8080

   # Image server (ComfyUI)
   comfyui_python = '<tools>\ComfyUI_windows_portable\python_embeded\python.exe'
   comfyui_main = '<tools>\ComfyUI_windows_portable\ComfyUI\main.py'
   comfyui_port = 8188
   comfyui_model_root = '<tools>\ComfyUI_windows_portable\ComfyUI\models'

   # gpu_vram_gb = 16

   default_chat_model = "gemma-4-12b"
   default_image_model = "z-image-turbo"

   [model_dirs]
   "gemma-4-12b" = '<tools>\models\gemma-4-12b'
   ```

Every key:

| Key | What it does |
|---|---|
| `llama_server_exe` | Path to `llama-server` (`llama-server.exe` on Windows). With this and a `model_dirs` entry for the default chat model, Hearthkeep starts and stops llama-server itself. |
| `llama_port` | The port llama-server listens on, on `127.0.0.1`. Default 8080. |
| `comfyui_python` | The Python that runs ComfyUI. In the portable build, `python_embeded\python.exe`. |
| `comfyui_main` | ComfyUI's `main.py`. |
| `comfyui_port` | The port ComfyUI listens on, on `127.0.0.1`. Default 8188. |
| `comfyui_model_root` | A folder with `diffusion_models`, `text_encoders` and `vae` inside. The image cards' file names are looked up there. Images are on only when all three `comfyui_*` paths are set. |
| `gpu_vram_gb` | Optional. Your GPU memory in GB. Hearthkeep reads it from `nvidia-smi`; if that fails it assumes 16 GB and logs a warning. Set it when the guess is wrong. |
| `default_chat_model` | The card id of the chat model to use. Default `gemma-4-12b`. |
| `default_image_model` | The card id of the image model to use when you don't ask for one. Default `z-image-turbo`. |
| `[model_dirs]` | For each chat card id, the folder that holds its files. |

A value that is empty or starts with `<` counts as not set. An unknown key or a wrong type stops the server at start-up with a message naming the key. More detail: [docs/models.md](models.md#modelstoml).

## 7. Decide on remote access before your first passkey

A passkey belongs to one web address. Hearthkeep calls that address `public_origin`, and the passkey's domain `rp_id`. A passkey created on `http://localhost:8765` **does not work** on `https://<machine>.<tailnet>.ts.net`, and the other way round. Once a passkey exists, the first-device enrollment code no longer works.

So decide now:

- **Local only.** You use Hearthkeep on this PC only. Keep the defaults and go to step 8.
- **Tailscale.** You want to use it from your phone or Mac. Do the steps below first, and use the Tailscale address on every device, **this PC included**.

### Option A: local only

Leave `<data>\config.toml` as it is created on first start:

```toml
public_origin = "http://localhost:8765"
rp_id = "localhost"
```

Open exactly `http://localhost:8765`. `http://127.0.0.1:8765` is a different origin, and sign-in and every change will fail there.

### Option B: Tailscale

1. Install Tailscale on this PC and sign in: [tailscale.com/download](https://tailscale.com/download).
2. In the Tailscale admin console, under **DNS**, turn on **MagicDNS** and **HTTPS Certificates**. `tailscale serve` needs both to give you an HTTPS address.
3. Serve Hearthkeep's local port over HTTPS on your tailnet. In a terminal on this PC:

   ```
   tailscale serve --bg --https=443 http://127.0.0.1:8765
   ```

   `--bg` keeps it running after you close the terminal and across restarts, until you turn it off.

4. Check it and read your address:

   ```
   tailscale serve status
   ```

   It shows the `https://<machine>.<tailnet>.ts.net` address that now proxies to `http://127.0.0.1:8765`. That is your address.

5. Start Hearthkeep once so it creates `<data>\config.toml` (step 8, item 1), stop it with Ctrl+C, and **don't** create a passkey yet.
6. Edit `<data>\config.toml`:

   ```toml
   public_origin = "https://<machine>.<tailnet>.ts.net"
   rp_id = "<machine>.<tailnet>.ts.net"
   ```

   No trailing slash and no port. `rp_id` must equal the host name in `public_origin`, or the server refuses to start. Keep `host = "127.0.0.1"` and `port = 8765`.

7. Install Tailscale on your phone and Mac, and sign in to the same tailnet.
8. From now on, open `https://<machine>.<tailnet>.ts.net` on every device, including this PC.

Rules:

- **Never use Tailscale Funnel.** Funnel puts Hearthkeep on the public internet.
- **Never forward a port on your router**, and never change `host` to anything but `127.0.0.1`. Hearthkeep refuses non-loopback addresses anyway.

To turn serving off later:

```
tailscale serve --bg --https=443 http://127.0.0.1:8765 off
```

## 8. First start and your passkey

1. Start the server:

   ```
   cd <repo>\server
   uv run hearthkeep serve --data <data>
   ```

   On first start it creates the data folder layout and `<data>\config.toml`. It prints the model cards it loaded, how chat and images are set up, and a one-time code:

   ```
   First-device enrollment code: ABCD2345
   ```

2. Open your address: `http://localhost:8765` (option A) or `https://<machine>.<tailnet>.ts.net` (option B).
3. Enter the code within **15 minutes**, give the device a name, and choose **Add this device**.
4. Create the passkey when your browser asks (Windows Hello, Touch ID, Face ID or a PIN). You are now signed in.
5. Send a message in Chat. The first reply takes a little longer while llama-server loads the model.

If the code expired, or you lost it, create a new one (it works only while no passkey exists):

```
uv run hearthkeep enroll-code --data <data>
```

### Have your passkey on a second device before you rely on it

If your only passkey is lost, you cannot sign in. Make sure the passkey is on a second device (your phone or Mac) before you depend on Hearthkeep.

**Phase 2 limit:** the server can issue a code to add another device (an already signed-in device asks for it with a passkey check), but the web app has no screen for it yet. Until it has, the practical way to reach Hearthkeep from more than one device is a passkey that syncs:

- When the browser asks where to save the passkey, choose a passkey manager that syncs between your devices, for example iCloud Keychain (iPhone and Mac), Google Password Manager, or a password manager that supports passkeys.
- On Windows you can choose **iPhone, iPad or Android device** and scan the QR code with your phone. The passkey is then saved on the phone, and syncs to your Mac if you use iCloud Keychain.

Then open your Tailscale address on the second device and check that you can sign in. The last remaining device can never be removed, so you cannot lock yourself out from inside the app.

## 9. Optional: an API key for an external chat server

Only for when Hearthkeep uses a chat server you run yourself (no `models.toml`) and that server needs a key. When Hearthkeep runs llama-server itself, it generates and stores its own key under the same name; you don't need to do anything.

1. Store the key. The value is read from a hidden prompt, so it never appears in your shell history:

   ```
   uv run hearthkeep secrets set chat_api_key --data <data>
   ```

2. Check that it is there (this prints names only, never values):

   ```
   uv run hearthkeep secrets list --data <data>
   ```

3. Restart Hearthkeep.

The secret's name is set by `chat_api_key_secret` in `config.toml` (default `chat_api_key`). Never put the key itself in `config.toml`.

## 10. Run at login (Windows)

Do this after your passkey works. Hearthkeep then starts by itself when you sign in to Windows, with no window.

The task runs as **your own Windows user**: the key that decrypts your secrets is in your user's keyring.

1. Turn it on:

   ```
   cd <repo>\server
   uv run hearthkeep startup enable --data <data>
   ```

   This creates a Task Scheduler task named `Hearthkeep` that runs at your logon (`pythonw.exe -m hearthkeep serve`, so no window opens) and keeps its output in `<data>\logs\hearthkeep.log`.

2. Check it, or turn it off again:

   ```
   uv run hearthkeep startup status --data <data>
   uv run hearthkeep startup disable --data <data>
   ```

   Settings in the app shows the same state and the command to copy. The app only displays it; it never changes the task.

3. Sign out and back in (or restart) and open your address to test it.

**Logs.** Because there is no window, everything the server prints goes to `<data>\logs\hearthkeep.log`. While no passkey exists yet, the first-device enrollment code is written there too. If you start Hearthkeep this way before your first sign-in, run `uv run hearthkeep enroll-code --data <data>` to get a code (it works only while no passkey exists).

**Stopping Hearthkeep also stops the models it started.** llama-server and ComfyUI run as Hearthkeep's child processes; on Windows they are tied to it so they end even if Hearthkeep is killed. To stop everything, end the `Hearthkeep` task in Task Scheduler (right-click, **End**).

`tailscale serve --bg` needs no task of its own: Tailscale keeps the setting.

### If `startup enable` does not work: make the task by hand

Create the task yourself in Task Scheduler:

1. Open **Task Scheduler** and choose **Create Task** (not "Create Basic Task").
2. **General** tab: name it `Hearthkeep`. Choose **Run only when user is logged on**.
3. **Triggers** tab: **New**, then **Begin the task: At log on**, for your user.
4. **Actions** tab: **New**, then **Start a program**:

   | Field | Value |
   |---|---|
   | Program/script | `<repo>\server\.venv\Scripts\pythonw.exe` |
   | Add arguments | `-m hearthkeep serve --data "<data>" --log-file "<data>\logs\hearthkeep.log"` |
   | Start in | `<repo>\server` |

5. **Settings** tab: clear **Stop the task if it runs longer than**.
6. Choose **OK**, then right-click the task and choose **Run** to test it. Open your address and sign in.

The same task from PowerShell:

```powershell
$action = New-ScheduledTaskAction -Execute '<repo>\server\.venv\Scripts\pythonw.exe' -Argument '-m hearthkeep serve --data "<data>" --log-file "<data>\logs\hearthkeep.log"' -WorkingDirectory '<repo>\server'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName 'Hearthkeep' -Action $action -Trigger $trigger -Settings $settings
```

**macOS and Linux:** `startup` is built in only for Windows. Use a launchd agent or a systemd user service that runs `hearthkeep serve --data <data> --log-file <data>/logs/hearthkeep.log`. Untested.

## 11. Python sandbox (Docker)

Hearthkeep can read the files you attach to a chat and analyse them with Python. The code always runs inside a Docker container with no network and read-only access to that chat's files (the exact limits are in [security.md](security.md#the-python-sandbox)). Without Docker the chat still works, but Python asks you first on every call and then answers that it needs Docker: Hearthkeep never runs analysis code on your PC itself.

1. Install **Docker Desktop** from [docker.com](https://www.docker.com/products/docker-desktop/) (on Windows, `winget install --id Docker.DockerDesktop -e` also works), start it, and wait until it says it is running. Check:

   ```
   docker version
   ```

2. Build the sandbox image. This is a **one-time download of about 1 GB** (Python and the analysis libraries), so do it on a connection you are happy to use:

   ```
   cd <repo>\server
   uv run hearthkeep sandbox build --data <data>
   ```

   Hearthkeep never builds or downloads the image by itself.

3. Check Docker and the image:

   ```
   uv run hearthkeep sandbox status --data <data>
   ```

   It prints `Docker: running` and `Sandbox image: hearthkeep-sandbox:<hash> (present)`. If the image shows as `missing`, run the build again.

Docker Desktop has to be running whenever you want to analyse a file. After an update that changes the sandbox, `sandbox status` shows the image as missing; run `sandbox build` again.

**Run `hearthkeep sandbox build` again after updating.** The image's tag is a hash of its files, so an update that changes the sandbox (the next one does: it reads dates written day/month/year and finds gaps in dated data) needs a new image. Until you build it, Python answers that the sandbox isn't built yet.

## 12. Notifications on your phone

When the assistant sends you a file or a summary, it appears in the tray in the app. You can also get a notification that says "A file is ready in Hearthkeep". It carries no content, only that nudge.

**Android (Chrome or Samsung Internet).** Open Hearthkeep on the phone, then turn notifications on in **Settings** or from the tray, and allow notifications when the browser asks. The push is delivered through Google's push service, which sees only your phone's push address and a signature from your server, never the file or its name. See [security.md](security.md#notifications-hearthkeeps-only-outgoing-connection).

**iPhone and iPad.** Add Hearthkeep to the Home Screen first (Share, then **Add to Home Screen**) and open it from there; Safari only allows notifications for apps added this way. This is not the tested path, so treat it as best effort. Notifications go through Apple's push service on the same terms.

Notifications are opt-in per device and can be turned off in the same place. Your PC must be on and Hearthkeep running for anything to be sent.

## 13. Updating

1. Stop Hearthkeep (Ctrl+C, or end the scheduled task).
2. Pull the new version:

   ```
   cd <repo>
   git pull
   ```

3. Rebuild the web app:

   ```
   cd <repo>\web
   npm ci
   npm run build
   ```

4. Update the server's dependencies:

   ```
   cd <repo>\server
   uv sync
   ```

5. If you analyse files with Python, run `hearthkeep sandbox build` again after updating (the sandbox image changes with the code):

   ```
   uv run hearthkeep sandbox build --data <data>
   ```

6. Start Hearthkeep again. Read the start-up output: it lists any card or config problems.

Your data folder is not touched by an update. Compare `<repo>\config.example.toml` and `<repo>\models.example.toml` with your own copies for new settings.

## 14. Backing up

Stop Hearthkeep before you copy, so the database is not being written.

What matters in `<data>`:

| Path | Why |
|---|---|
| `hearthkeep.db` | Chats, passkeys, approvals, audit log, permission levels. The most important file. |
| `config.toml`, `models.toml` | Your settings. |
| `models\cards\` | Your own model cards, if any. |
| `secrets\` | Your encrypted secrets. Useless without the key in the keyring (below). |
| `workspace\output\` | Images and files the assistant made. |
| `workspace\uploads\` | Photos you uploaded. |
| `workspace\chats\` | Each chat's files and analysis runs |

`logs\` and `runtime\` can be left out; they are recreated. Restoring a backup on another PC also needs Docker and the sandbox image there (step 11).

**The secret key.** Secrets are encrypted with a key held in your operating system's keyring (Windows Credential Manager, macOS Keychain, or the Secret Service on Linux), under the service name `hearthkeep`. The entry's name is derived from the data folder's full path. That means:

- Restoring to the same path, for the same user on the same PC, just works.
- Restoring to another path or another PC, Hearthkeep cannot find the key and stops with "master key is missing from the OS keyring". The fix: delete `<data>\secrets\store.bin`, start Hearthkeep, and set your secrets again (step 9). When Hearthkeep runs llama-server itself, it creates a new key on its own.

Your passkeys stay valid after a restore, as long as `public_origin` and `rp_id` are unchanged.

## 15. Troubleshooting

Start with the logs in `<data>\logs\`: `llama-server.log` for the chat model, `comfyui.log` for images, and `hearthkeep.log` when Hearthkeep starts at login. Model-specific problems are covered in [docs/models.md](models.md#troubleshooting).

**Port 8765 is already in use.** Another program (or a second copy of Hearthkeep) is on the port. Find it:

```
netstat -ano | findstr :8765
```

Stop that program, or change `port` in `<data>\config.toml`. For a local-only setup, change the port in `public_origin` too. With Tailscale, turn the old `tailscale serve` off (step 7) and run it again with the new port; `public_origin` stays the same. For llama-server or ComfyUI, change `llama_port` or `comfyui_port` in `models.toml`.

**The enrollment code expired.** Codes last 15 minutes. Restart the server, or run `uv run hearthkeep enroll-code --data <data>`. If it says a passkey is already enrolled, the code is no longer needed: sign in with your passkey.

**The passkey was created on the wrong address.** For example, you made it on `localhost` and then switched to Tailscale. The browser offers no passkey, or sign-in fails. Because a passkey exists, `enroll-code` refuses. Your choices:

- Switch `public_origin` and `rp_id` back to the address where you created the passkey, and keep using that.
- Or start over: stop Hearthkeep, move the old data folder aside, create a new empty one with the right settings (step 7) before the first start, and enroll again. Your old chats stay in the old folder.

Also check that you open exactly the address in `public_origin` (`localhost`, not `127.0.0.1`; no other port).

**Sign-in or every action fails with a 403.** The page was opened on a different address than `public_origin`. Open exactly that address.

**The chat model won't start.** Open `<data>\logs\llama-server.log`. The usual causes:

- `llama_server_exe` or the `[model_dirs]` folder is wrong, or a file name doesn't match the card.
- On Windows, the CUDA runtime zip was not extracted next to `llama-server.exe`.
- Another program is using the GPU memory. Close it and try again.
- The path is too long (see step 3).
- The start-up output says "using the chat server at chat_base_url": Hearthkeep did not find `llama_server_exe` or the `model_dirs` entry for the default chat model, so it is not running llama-server.

**Images are off, or the image tool is missing.** The start-up output says "Images: not set up". Set all three `comfyui_*` paths in `<data>\models.toml`, check they point to real files, and restart. If ComfyUI starts but fails, read `comfyui.log`: usually a missing weight file, or a ComfyUI too old for the shipped workflows.

**Python says it needs Docker.** Docker Desktop is not running (or not installed). Start it, wait until it is running, check with `uv run hearthkeep sandbox status --data <data>`, and ask again. Hearthkeep does not run the code on your PC instead.

**The sandbox image is missing.** `sandbox status` shows `Sandbox image: ... (missing)`. Build it once with `uv run hearthkeep sandbox build --data <data>` (about 1 GB, step 11). It also goes missing after an update that changes the sandbox.

**Hearthkeep does not start at login.** Run `uv run hearthkeep startup status --data <data>`; if it says off, run `startup enable`. Then read `<data>\logs\hearthkeep.log` for the reason.

**"config error" at start-up.** The message names the key. Common causes: a non-loopback `host`, an `http://` `public_origin` that is not localhost, `rp_id` not matching `public_origin`, or a `chat_base_url` that is not on this PC.

**"secret store error".** The keyring key is missing or doesn't match. See [Backing up](#14-backing-up). On Linux, a keyring service (such as GNOME Keyring) must be running.

## 16. Uninstalling

1. Stop Hearthkeep. If you turned on start at login, turn it off (or, if you made the task by hand, delete it):

   ```
   uv run hearthkeep startup disable --data <data>
   Unregister-ScheduledTask -TaskName 'Hearthkeep'
   ```

   Use the second command only for a task you made by hand.

2. Turn off Tailscale serving:

   ```
   tailscale serve --bg --https=443 http://127.0.0.1:8765 off
   ```

3. Delete the repository folder `<repo>`.
4. If you don't want your data any more, delete `<data>`. Back it up first if you might.
5. Remove the keyring entry: in Windows **Credential Manager**, under **Windows Credentials**, remove the generic credential for `hearthkeep`. On macOS, use Keychain Access; on Linux, your keyring app.
6. Remove the passkey from each device's passkey manager (Windows: **Settings > Accounts > Passkeys**; Apple devices: the Passwords app; other managers: their own list).
7. Optionally, delete llama.cpp, ComfyUI and the model files in `<tools>`.
8. Optionally, remove the sandbox image: `docker image rm hearthkeep-sandbox:<hash>` (read the tag from `hearthkeep sandbox status` before you delete anything).
