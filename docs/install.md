# Installing Hearthkeep

This guide takes you from a fresh PC to Hearthkeep running with its own models, reachable from your phone or Mac. Windows 11 with an NVIDIA GPU is the tested host; chat also runs on the processor alone, only more slowly. Notes for macOS and Linux are given where they differ; as a host, both are untested.

Placeholders used below:

- `<repo>`: the folder you cloned Hearthkeep into.
- `<data>`: your Hearthkeep data folder (step 3).
- `<machine>.<tailnet>.ts.net`: your PC's Tailscale name (step 7).

Commands are for PowerShell on Windows unless marked otherwise. Run each command on its own line.

Contents:

1. [Install the prerequisites](#1-install-the-prerequisites)
2. [Clone and build](#2-clone-and-build)
3. [Create a data folder](#3-create-a-data-folder)
4. [Chat model](#4-chat-model)
5. [Image models (optional)](#5-image-models-optional)
6. [Models you already have](#6-models-you-already-have)
7. [Decide on remote access before your first passkey](#7-decide-on-remote-access-before-your-first-passkey)
8. [First start and your passkey](#8-first-start-and-your-passkey)
9. [Use a model server you already run](#9-use-a-model-server-you-already-run)
10. [Run at login (Windows)](#10-run-at-login-windows)
11. [Python sandbox (Docker)](#11-python-sandbox-docker)
12. [Optional: GitHub](#12-optional-github)
13. [Notifications on your phone](#13-notifications-on-your-phone)
14. [Updating](#14-updating)
15. [Backing up](#15-backing-up)
16. [Troubleshooting](#16-troubleshooting)
17. [Uninstalling](#17-uninstalling)

## 1. Install the prerequisites

You need:

| Tool | Version | Where to get it |
|---|---|---|
| Git | 2.31 or newer (the GitHub tools need it; without it they stay off) | [git-scm.com](https://git-scm.com/downloads) |
| Python | 3.12 or newer | [python.org](https://www.python.org/downloads/) (uv can also install it for you) |
| uv | any recent | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |
| Node.js | 20.19+ or 22.12+ | [nodejs.org](https://nodejs.org/) (the LTS release is fine) |
| NVIDIA driver (optional) | recent, with `nvidia-smi` | [nvidia.com/drivers](https://www.nvidia.com/Download/index.aspx); needed for the fast chat engine and for image models |
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

   `node --version` must print 20.19 or later (or 22.12 or later). With an NVIDIA graphics card, `nvidia-smi` must list it; Hearthkeep uses it to read how much graphics memory you have and to recommend a model. Without one, skip that command.

**macOS and Linux:** install the same tools with your package manager (for example Homebrew on macOS). `nvidia-smi` only exists with an NVIDIA GPU; without it, Hearthkeep recommends the small model on the processor (to override the memory it detects, see `gpu_vram_gb` in [docs/models.md](models.md#modelstoml)).

## 2. Clone and build

The quick way on Windows needs none of the commands below:

1. Download the Hearthkeep zip from the latest release on GitHub.
2. Unzip it to a short path, for example `C:\Hearthkeep\app`.
3. Double-click `Setup.cmd`. It installs uv for your Windows account if needed (no admin rights), keeps your data in `C:\Hearthkeep\data`, starts Hearthkeep and opens the setup page in your browser, where you create your first passkey.

If you'll use Hearthkeep from your phone through Tailscale, read [step 7](#7-decide-on-remote-access-before-your-first-passkey) before you run Setup.cmd.

The rest of this guide is the manual way: building from a clone, choosing your own data folder, and setting up the models, Docker, Tailscale and GitHub by hand.

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

1. Pick a short path. On Windows, for example `C:\Hearthkeep\data`, the folder `Setup.cmd` uses.
2. Create it:

   ```
   mkdir C:\Hearthkeep\data
   ```

3. Use that path wherever this guide says `<data>`.

Keep the path short. llama-server on Windows cannot open a file whose full path is 260 characters or longer. Hearthkeep writes the chat model's key file to `<data>\runtime\llama-api-key` and checks this path (and the model file paths) before it starts llama-server. If any is too long it refuses with "Hearthkeep's data folder path is too long for llama-server on Windows". ComfyUI is not affected.

The programs and models you download from Settings (steps 4 and 5) also live inside this folder, so it needs room: from about 4 GB for the smallest chat model to roughly 50 GB with every model and ComfyUI. Hearthkeep checks the free space before it downloads anything.

Choose the location once. The key that encrypts your secrets is stored in the OS keyring under a name derived from the data folder's full path. If you move the folder later, see [Backing up](#15-backing-up).

## 4. Chat model

Hearthkeep downloads its own chat model from Settings. There is nothing to install by hand, and you do this after you have created your passkey ([step 8](#8-first-start-and-your-passkey)). Until then Hearthkeep runs, but chat says that no chat model is set up yet.

1. Open **Settings › Chat model**. The top line names your graphics card and how much memory it has, and one model carries the badge **Recommended for this PC**.
2. Choose a model and select **Download**. Hearthkeep lists everything it will fetch, with the sizes and a link to each licence. Confirm with your passkey.
3. Watch the progress list. You can leave the page; the download carries on, and the list is there when you come back.
4. When it finishes, Hearthkeep asks you to restart. Select **Restart now**. If you started Hearthkeep from a terminal rather than from the Start menu, the page says "Restart Hearthkeep to use it." instead; stop it and start it again by hand.
5. Send a message in Chat. The first reply takes a little longer while the model loads.

**What each model needs**

| Model | Download | Runs on | Notes |
|---|---|---|---|
| Qwen3.5 4B | 3.0 GB | The processor alone, or under 6 GB of graphics memory | The smallest. Chosen for you when there is no NVIDIA graphics card. |
| Qwen3.5 9B | 6.2 GB | About 8 GB of graphics memory | A good all-rounder. |
| Gemma 4 12B | 10.4 GB | 11 GB of graphics memory or more | Can also look at pictures and scanned pages. The download includes its vision file. |

The first model you download also fetches the program that runs it, the **chat engine** (under 0.7 GB). The **Chat engine** list below the models shows the builds on offer: one for NVIDIA graphics cards, one for other graphics cards (marked as an alternative) and one for the processor alone. The recommended one is picked from your PC. Hearthkeep installs one engine at a time, and a model keeps working with whichever you picked.

A model that needs more graphics memory than your PC has is greyed out with the reason. A PC without an NVIDIA graphics card can still chat with the processor alone, only more slowly.

**How long it takes.** The time depends on your connection. At 10 MB a second, the 4B model takes about five minutes and the 12B model about 17. Keep the PC awake and online while it runs.

**If it stops.** A broken connection, a restart of Hearthkeep or the PC, or **Cancel** never loses what has arrived. Select **Download** again (or **Retry** in the list) and it carries on from where it stopped. A file that failed on the network is also tried again by itself a few times first. Every file is checked against a checksum before it is used, and a file that doesn't match is thrown away and reported, never used. Hearthkeep fetches only from GitHub and Hugging Face, and only the files in its own list.

**Disk space.** Downloads land inside Hearthkeep's data folder ([step 3](#3-create-a-data-folder)). Hearthkeep checks there is room before it starts and says if there isn't.

**Switching.** Models you have downloaded stay on the PC. Select **Use this** on one of them to switch, then restart when asked. Nothing is deleted.

**Licences.** All three models are released under Apache 2.0 and the chat engine under the MIT licence. Each row in Settings links to its licence before you download:

- [Qwen3.5 4B](https://huggingface.co/Qwen/Qwen3.5-4B) and [Qwen3.5 9B](https://huggingface.co/Qwen/Qwen3.5-9B)
- [Gemma 4 12B](https://huggingface.co/google/gemma-4-12B-it)
- [llama.cpp](https://github.com/ggml-org/llama.cpp/blob/b12099/LICENSE)

**A model server you already run.** If you already run an OpenAI-compatible chat server on this PC (llama.cpp, Ollama, LM Studio), you don't need to download anything: see [step 9](#9-use-a-model-server-you-already-run).

**macOS and Linux:** the downloads are Windows builds. See "Doing it by hand" in [docs/models.md](models.md#doing-it-by-hand) for your own llama.cpp, and step 9 for a server you run yourself. Both are untested as a Hearthkeep host.

## 5. Image models (optional)

Skip this step if you only want chat. Image models need an NVIDIA graphics card; without one, Settings says so and the downloads can't be chosen.

1. Open **Settings › Image models**.
2. Choose a model and select **Download**, then confirm with your passkey. Hearthkeep lists what it will fetch, with the sizes and a link to each licence.

   | Model | Download | Good at |
   |---|---|---|
   | Z-Image Turbo | 20.7 GB | Pictures that contain words, and photo-like scenes |
   | FLUX.2 klein 4B | 16.1 GB | Photos and edits to a photo you attach |

   Both need about 10 to 10.5 GB of graphics memory while a picture is made. If you pick both, the text file they share is downloaded once.
3. The first image model also fetches the picture maker, **ComfyUI** (1.9 GB), the program that draws the pictures. It is released under GPL-3.0: [licence](https://github.com/Comfy-Org/ComfyUI/blob/v0.44.0/LICENSE). The image models are released under Apache 2.0: [Z-Image Turbo](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo).
4. When it finishes, restart as asked, as in step 4.

Downloads resume after an interruption and are checked against checksums, as for chat models. Hearthkeep unpacks ComfyUI itself, using the archive tool that comes with Windows.

You don't need to start ComfyUI. Hearthkeep starts it when the first image is needed. It unloads the chat model while a picture is made (see [GPU switching](models.md#gpu-switching)). If you already have your own ComfyUI and image files, Hearthkeep keeps using them: see "Doing it by hand" in [docs/models.md](models.md#doing-it-by-hand).

## 6. Models you already have

Nothing you have set up by hand is lost. If your PC already has a working hand-made setup for Hearthkeep's models, Hearthkeep reads it as before. A model it finds there shows as installed in Settings, and it is never downloaded again. Hearthkeep changes only the few settings it manages, keeps your comments, and saves a copy of your file before its first change.

Developers and anyone who prefers to do it all by hand: the manual route, with every setting explained, is in [docs/models.md](models.md#doing-it-by-hand).

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

5. Tell Hearthkeep your Tailscale address before the first passkey is made.

   **With `Setup.cmd`** (do this before you double-click it):

   1. Make the folder `C:\Hearthkeep\data`, the data folder Setup uses.
   2. In it, make a text file named `config.toml` with these two lines, but with your own address from item 4. In this example the PC's address is `https://homepc.tail1234.ts.net`:

      ```toml
      public_origin = "https://homepc.tail1234.ts.net"
      rp_id = "homepc.tail1234.ts.net"
      ```

      In Notepad, choose **Save as type: All files** so the name stays `config.toml` and doesn't become `config.toml.txt`.
   3. Double-click `Setup.cmd`. Setup keeps your `config.toml` and opens its setup page on your Tailscale address, so your first passkey is made for that address.

   **With a manual start:** start Hearthkeep once so it creates `<data>\config.toml` (step 8, item 1), stop it with Ctrl+C, and **don't** create a passkey yet. Then edit `<data>\config.toml`:

   ```toml
   public_origin = "https://<machine>.<tailnet>.ts.net"
   rp_id = "<machine>.<tailnet>.ts.net"
   ```

   Either way: no trailing slash and no port. `rp_id` must equal the host name in `public_origin`, or the server refuses to start. Keep `host = "127.0.0.1"` and `port = 8765`; a `config.toml` without them, like the one above, uses exactly those.
6. Leave `tailscale serve` on: the setup page and every later visit go through it.

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

If you used `Setup.cmd` (step 2), your first passkey is normally made on the setup page it opens (on your Tailscale address if you wrote `config.toml` first, as in step 7), and you can skip to [Have your passkey on a second device](#have-your-passkey-on-a-second-device-before-you-rely-on-it). The steps below are for a manual start.

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
5. Download a chat model in **Settings › Chat model** ([step 4](#4-chat-model)), restart when asked, and send a message in Chat. The first reply takes a little longer while the model loads.

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

## 9. Use a model server you already run

Only for when you already run a chat server on this PC and want Hearthkeep to talk to it instead of downloading a model. It can be llama.cpp, Ollama, LM Studio or anything else that speaks the same OpenAI-style chat protocol.

1. Open **Settings › Chat model** and scroll to **Use a model server I already run**.
2. Type the server's address in **Server address**. It must be a server on this PC, so the address starts with `http://localhost` or `http://127.0.0.1`; Hearthkeep refuses any other address.
3. If the server needs a key, type it in **Key (optional)**. Hearthkeep keeps it in its encrypted secret store, never in a settings file. Leave it empty to keep the key saved before.
4. Select **Use this server** and confirm with your passkey, then restart when asked.

Your downloaded models stay where they are. To go back to one of them, select **Use this** on it in the same screen and restart.

## 10. Run at login (Windows)

Do this after your passkey works. Hearthkeep then starts by itself when you sign in to Windows, with no window.

The task runs as **your own Windows user**: the key that decrypts your secrets is in your user's keyring.

1. Turn it on:

   ```
   cd <repo>\server
   uv run hearthkeep startup enable --data <data>
   ```

   This creates a Task Scheduler task named `Hearthkeep` that runs at your logon (`pythonw.exe -m hearthkeep launch`, so no window opens). The task starts Hearthkeep's launcher, which starts the server and starts it again if it stops or is restarted from Settings. Its output goes to `<data>\logs\hearthkeep.log` and `<data>\logs\launcher.log`.

2. Check it, or turn it off again:

   ```
   uv run hearthkeep startup status --data <data>
   uv run hearthkeep startup disable --data <data>
   ```

   Settings in the app shows the same state and the command to copy. The app only displays it; it never changes the task.

3. Sign out and back in (or restart) and open your address to test it.

**Logs.** Because there is no window, everything the server prints goes to `<data>\logs\hearthkeep.log`, and the launcher's own notes (starts, restarts, crashes) go to `<data>\logs\launcher.log`. The enrollment code is never written there. While no passkey exists yet, the log says so and Hearthkeep doesn't keep running in the background; double-click Setup.cmd in the Hearthkeep folder to finish setting up.

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
   | Add arguments | `-m hearthkeep launch --data "<data>"` |
   | Start in | `<repo>\server` |

5. **Settings** tab: clear **Stop the task if it runs longer than**.
6. Choose **OK**, then right-click the task and choose **Run** to test it. Open your address and sign in.

The same task from PowerShell:

```powershell
$action = New-ScheduledTaskAction -Execute '<repo>\server\.venv\Scripts\pythonw.exe' -Argument '-m hearthkeep launch --data "<data>"' -WorkingDirectory '<repo>\server'
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

2. Build the sandbox images. This is a **one-time download of about 1 GB** (Python and the analysis libraries, plus a second, smaller image for GitHub repos, step 12), so do it on a connection you are happy to use:

   ```
   cd <repo>\server
   uv run hearthkeep sandbox build --data <data>
   ```

   The command builds two images: `hearthkeep-sandbox` (Python analysis) and `hearthkeep-repo` (running a GitHub repo's tests and installing its dependencies). Hearthkeep never builds or downloads an image by itself.

   **Settings › Add-ons** shows whether Analysis is ready, and what is missing if it is not.

3. Check Docker and the image:

   ```
   uv run hearthkeep sandbox status --data <data>
   ```

   It prints `Docker: running`, then `Sandbox image: hearthkeep-sandbox:<hash> (present)` and `Repo image: hearthkeep-repo:<hash> (present)`. If an image shows as `missing`, run the build again.

**Run one Hearthkeep per Docker install.** At start-up Hearthkeep removes containers and dependency caches it doesn't recognise as its own (its sandbox containers and the `hk-deps-*` volumes of other data folders), so a second Hearthkeep on the same Docker would lose its running containers and cached dependencies.

Docker Desktop has to be running whenever you want to analyse a file. After an update that changes the sandbox, `sandbox status` shows the image as missing; run `sandbox build` again.

**Run `hearthkeep sandbox build` again after updating.** The image's tag is a hash of its files, so an update that changes a sandbox needs a new image. Until you build it, Python answers that the sandbox isn't built yet. The same goes for the repo image and GitHub tests.

## 12. Optional: GitHub

Hearthkeep can work on GitHub repos you choose: read them, change them on a branch of its own, run their tests, and push or open a pull request after you approve. It needs Git 2.31 or newer on this PC (step 1), a token you make on GitHub, and, to run tests, Docker and the repo image (step 11). Without Git or without a saved token, the GitHub tools are not offered to the assistant at all.

**Settings › Add-ons** shows whether the GitHub add-on is ready, and what is missing if it is not.

What it does and never does is in [security.md](security.md#github).

### 12.1 Make a token

Use a **fine-grained personal access token**. Classic tokens (they start with `ghp_`) are refused.

1. On GitHub, open **Settings > Developer settings > Personal access tokens > Fine-grained tokens** and choose **Generate new token**.
2. Set **Repository access** to **Only select repositories** and pick just the repos you want Hearthkeep to work on. Do not choose *All repositories*.
3. Under **Repository permissions**, set:

   | Permission | Access |
   |---|---|
   | Contents | Read and write |
   | Pull requests | Read and write |
   | Issues | Read and write |
   | Commit statuses | Read-only |
   | Checks | Read-only |
   | Metadata | Read-only (always on) |

4. Set an expiry. Thirty to ninety days is a sensible range: a short-lived token limits the damage if it ever leaks, and GitHub emails you before it ends. Put the renewal date in your calendar.
5. Generate the token and copy it. GitHub shows it once.

### 12.2 Save it and add repos

1. Open Hearthkeep, go to **Settings > GitHub**, paste the token and choose **Save token**. Hearthkeep asks for your passkey, checks the token with GitHub, and only then stores it encrypted in the secret store. The same place lets you replace the token or remove it. Removing it needs no passkey.
2. Open the **Repos** view, type a repo as `<owner>/<repo>` and add it (passkey). Hearthkeep clones it into `<data>\workspace\repos\`. The repo must be one the token can reach.
3. In **Settings**, look at the permission levels. `GitHub` covers reading, editing and the outward actions; `GitHub tests` covers running tests. Both start at **Ask**. Outward actions always need a passkey, whatever the level.

To remove a repo, use **Remove** in the Repos view. It deletes the clone, including any work you have not pushed, so it asks for your passkey. If a clone cannot be deleted straight away, Hearthkeep finishes the job the next time it starts.

### 12.3 Using it

Ask in chat, for example: "In `<owner>/<repo>`, start a branch, fix the typo in the README, and run the tests." The assistant works only on branches named `hearthkeep/<name>`. Every approval card shows the full diff. Pushing, opening a pull request, commenting, merging and closing each wait for your approval and a passkey. A push sends exactly the commit shown on the card.

Running tests needs the repo's dependencies. The assistant asks to install them (`uv`, `pip` or `npm` projects only): that approval needs a passkey too, because the install container has full network access, like any program on your PC, including your home network. It sees only the dependency files, never your token or your other files. The installed dependencies are kept and reused until the lock file changes. The Repos view shows each repo's branch, changes and last test run, with the full test output.

## 13. Notifications on your phone

When the assistant sends you a file or a summary, it appears in the tray in the app. You can also get a notification that says "A file is ready in Hearthkeep". It carries no content, only that nudge.

**Android (Chrome or Samsung Internet).** Open Hearthkeep on the phone, then turn notifications on in **Settings** or from the tray, and allow notifications when the browser asks. The push is delivered through Google's push service, which sees only your phone's push address and a signature from your server, never the file or its name. See [security.md](security.md#notifications).

**iPhone and iPad.** Add Hearthkeep to the Home Screen first (Share, then **Add to Home Screen**) and open it from there; Safari only allows notifications for apps added this way. This is not the tested path, so treat it as best effort. Notifications go through Apple's push service on the same terms.

Notifications are opt-in per device and can be turned off in the same place. Your PC must be on and Hearthkeep running for anything to be sent.

## 14. Updating

1. Stop Hearthkeep. If it runs in the background (you open it from the Start menu, or it starts at login), open it and go to **Settings › General › Stop Hearthkeep**; you confirm with your passkey. If you started it by hand in a terminal, press Ctrl+C there.
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

5. If you analyse files with Python or use GitHub tests, run `hearthkeep sandbox build` again after updating (the sandbox images change with the code):

   ```
   uv run hearthkeep sandbox build --data <data>
   ```

6. Start Hearthkeep again. Read the start-up output: it lists any card or config problems.

The first start after an update brings an existing install up to date, once:

- on Windows, it notes where your data folder is, if nothing on this PC records that yet;
- on Windows, if start at login is on for this data folder and still runs Hearthkeep the old way, it is switched to the background launcher (start at login for another folder, or switched off, is left alone);
- on Windows, it makes the Start menu shortcut;
- on any system, it removes saved tool levels for tools that never shipped (`wallet`, `news`, `demo`).

Your config files, model cards, passkeys and your other tool levels are not touched. If a step fails (start at login or the shortcut), Hearthkeep still starts, and tries again the next time it starts, up to three starts; after that it stops trying and the note keeps saying what still needs you. For start at login, check it in Settings › Add-ons. For the shortcut, double-click `Setup.cmd` again: it makes the shortcut and opens Hearthkeep. When you next open the app, a one-time "Hearthkeep was updated" note lists anything that still needs you, such as a sandbox that needs rebuilding. Dismiss it once you've read it.

Compare `<repo>\config.example.toml` and `<repo>\models.example.toml` with your own copies for new settings.

## 15. Backing up

Stop Hearthkeep before you copy, so the database is not being written.

What matters in `<data>`:

| Path | Why |
|---|---|
| `hearthkeep.db` | Chats, passkeys, approvals, audit log, permission levels. The most important file. |
| `config.toml`, `models.toml` | Your settings, including which models you chose. |
| `models\chat\`, `models\image\`, `tools\` | The models and programs you downloaded from Settings. Large, and Hearthkeep can fetch them again, so back them up only if you'd rather not download them twice. |
| `models\cards\` | Your own model cards, if any. |
| `secrets\` | Your encrypted secrets. Useless without the key in the keyring (below). |
| `workspace\output\` | Images and files the assistant made. |
| `workspace\uploads\` | Photos you uploaded. |
| `workspace\chats\` | Each chat's files and analysis runs |

`logs\` and `runtime\` can be left out; they are recreated. Restoring a backup on another PC also needs Docker and the sandbox image there (step 11).

**The secret key.** Secrets are encrypted with a key held in your operating system's keyring (Windows Credential Manager, macOS Keychain, or the Secret Service on Linux), under the service name `hearthkeep`. The entry's name is derived from the data folder's full path. That means:

- Restoring to the same path, for the same user on the same PC, just works.
- Restoring to another path or another PC, Hearthkeep cannot find the key and stops with "master key is missing from the OS keyring". The fix: delete `<data>\secrets\store.bin`, start Hearthkeep, and set your secrets again (for a model server you run yourself, in Settings › Chat model, step 9). When Hearthkeep runs llama-server itself, it creates a new key on its own.

Your passkeys stay valid after a restore, as long as `public_origin` and `rp_id` are unchanged.

## 16. Troubleshooting

Start with the logs in `<data>\logs\`: `llama-server.log` for the chat model, `comfyui.log` for images, and `hearthkeep.log` when Hearthkeep starts at login. Model-specific problems are covered in [docs/models.md](models.md#troubleshooting).

**Port 8765 is already in use.** Another program (or a second copy of Hearthkeep) is on the port. Find it:

```
netstat -ano | findstr :8765
```

Stop that program, or change `port` in `<data>\config.toml`. For a local-only setup, change the port in `public_origin` too. With Tailscale, turn the old `tailscale serve` off (step 7) and run it again with the new port; `public_origin` stays the same. For llama-server or ComfyUI, change `llama_port` or `comfyui_port` in `<data>\models.toml` (add the line if it isn't there).

**The enrollment code expired.** Codes last 15 minutes. Restart the server, or run `uv run hearthkeep enroll-code --data <data>`. If it says a passkey is already enrolled, the code is no longer needed: sign in with your passkey.

**The passkey was created on the wrong address.** For example, you made it on `localhost` and then switched to Tailscale. The browser offers no passkey, or sign-in fails. Because a passkey exists, `enroll-code` refuses. Your choices:

- Switch `public_origin` and `rp_id` back to the address where you created the passkey, and keep using that.
- Or start over: stop Hearthkeep, move the old data folder aside, create a new empty one with the right settings (step 7) before the first start, and enroll again. Your old chats stay in the old folder.

Also check that you open exactly the address in `public_origin` (`localhost`, not `127.0.0.1`; no other port).

**Sign-in or every action fails with a 403.** The page was opened on a different address than `public_origin`. Open exactly that address.

**A download stops or fails.** Settings › Chat model (or Image models) shows the reason next to the item. A network error is tried again a few times by itself; after that, select **Retry** and it carries on from where it stopped. "There isn't enough free space on this PC" means the drive holding the data folder is too full: free some space, then retry. A checksum message means the file that arrived was not the file Hearthkeep expected, so it was removed: select **Retry** to fetch it again, and if it keeps happening, report it on the project's GitHub page. "Hearthkeep needs an update to fetch it" means a download address has changed; update Hearthkeep ([step 14](#14-updating)).

**The chat model won't start.** Open `<data>\logs\llama-server.log`. The usual causes:

- A model you chose by hand: `llama_server_exe` or the `[model_dirs]` folder is wrong, or a file name doesn't match the card. Downloading the model from Settings › Chat model avoids this.
- Another program is using the GPU memory. Close it and try again, or choose a smaller model in Settings › Chat model.
- The path is too long (see step 3).
- The start-up output says "using the chat server at chat_base_url" or "no chat model is set up yet": no chat model is chosen yet. Download one in Settings › Chat model, or point Hearthkeep at a server you run (step 9).

**Images are off, or the image tool is missing.** The start-up output says "Images: not set up". Download ComfyUI and an image model in Settings › Image models (step 5; it needs an NVIDIA graphics card) and restart. If you set the paths by hand, check that all three `comfyui_*` paths in `<data>\models.toml` point to real files. If ComfyUI starts but fails, read `comfyui.log`: usually a missing weight file, or a ComfyUI too old for the shipped workflows.

**Python says it needs Docker.** Docker Desktop is not running (or not installed). Start it, wait until it is running, check with `uv run hearthkeep sandbox status --data <data>`, and ask again. Hearthkeep does not run the code on your PC instead.

**The sandbox image is missing.** `sandbox status` shows `Sandbox image: ... (missing)`. Build it once with `uv run hearthkeep sandbox build --data <data>` (about 1 GB, step 11). It also goes missing after an update that changes the sandbox.

**The GitHub tools are missing, or Settings says "Install Git".** The assistant is offered GitHub tools only when Git 2.31 or newer is on the `PATH` and a token is saved. Open **Settings › Add-ons**: the GitHub card says what is missing. Restart Hearthkeep after installing or updating Git, then choose **Check again**, and save the token in Settings > GitHub.

**GitHub rejects the token, or a repo cannot be added.** The token must be fine-grained, unexpired, and allowed to reach that repo (*Only select repositories* must include it). Check the permissions in step 12.

**Tests say the dependencies are out of date, or the repo image is missing.** Ask the assistant to install them again (you approve it with your passkey), or run `uv run hearthkeep sandbox build --data <data>` if `sandbox status` shows the repo image as missing.

**Hearthkeep does not start at login.** Run `uv run hearthkeep startup status --data <data>`; if it says off, run `startup enable`. Then read `<data>\logs\hearthkeep.log` for the reason.

**"config error" at start-up.** The message names the key. Common causes: a non-loopback `host`, an `http://` `public_origin` that is not localhost, `rp_id` not matching `public_origin`, or a `chat_base_url` that is not on this PC.

**"secret store error".** The keyring key is missing or doesn't match. See [Backing up](#15-backing-up). On Linux, a keyring service (such as GNOME Keyring) must be running.

## 17. Uninstalling

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
7. Optionally, delete the models and programs you downloaded: the `models` and `tools` folders inside `<data>`, which go with it if you delete `<data>`. Files you downloaded by hand live wherever you put them.
8. Optionally, remove the sandbox images: `docker image rm hearthkeep-sandbox:<hash>` and `docker image rm hearthkeep-repo:<hash>` (read the tags from `hearthkeep sandbox status` before you delete anything). GitHub dependencies are kept in Docker volumes named `hk-deps-*`; remove them with `docker volume ls` and `docker volume rm`.
9. If you used GitHub, delete the token on GitHub (**Settings > Developer settings**), so it stops working there too.
