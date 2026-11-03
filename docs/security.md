# Hearthkeep security model (Phase 4)

This page describes what the code in this repository does today. Phase 1 is the secure core: sign-in, permissions, approvals, audit and secrets. Phase 2 adds local models and images: a process supervisor, GPU switching, image generation and photo uploads. Phase 3 adds chat files, allowed folders, Python analysis in a Docker sandbox, sending to your devices and start at login. Phase 4 adds GitHub: your repos, their tests in a second sandbox, and pushes and pull requests you approve. Budget, news and search arrive in later phases and are not part of this release.

`<data>` below means your own Hearthkeep data folder.

## Network model

- The server binds to loopback only (`127.0.0.1` or `::1`). A `host` in `config.toml` that is not a loopback address is rejected at startup.
- The port is set by `port` in `config.toml`. If you change it, change `public_origin` to the same port, and `rp_id` too if the host name changes: sign-in and the request checks below compare against them.
- The chat model must also be local: `chat_base_url` has to point at `127.0.0.1`, `::1` or `localhost`, otherwise the server refuses to start. Hearthkeep only talks to local models.
- Remote access is meant to go through your private Tailscale network (`tailscale serve` in front of the loopback port; see [install.md](install.md#7-decide-on-remote-access-before-your-first-passkey) for the setup). Nothing is exposed to the public internet. If `public_origin` is not localhost it must be `https`, and the session cookie is then marked `Secure`.
- The server ignores `X-Forwarded-For`, so a local process cannot fake its address to dodge rate limits.
- Hearthkeep never uses system or environment proxy settings for any of its connections (to the chat model, the image server and the push services), so your chats and the chat API key cannot be routed through a proxy or VPN.
- Hearthkeep's outgoing connections are the payload-free push notification described under [Notifications](#notifications), which is off until you turn it on for a device, and, once you save a GitHub token, the calls to `api.github.com` and `github.com` described under [GitHub](#github). Without a token, GitHub is never contacted.
- Every child process Hearthkeep starts (llama-server for chat, ComfyUI for images) binds to `127.0.0.1` only. A model card whose launch arguments try to bind another address (`0.0.0.0`, `::`, or a `--host` other than `127.0.0.1`) is rejected.
- ComfyUI on `127.0.0.1` has no authentication of its own, so any program on this PC can use it (render images, read its output, upload files). That is the same trust boundary as other local software: Hearthkeep does not protect you from programs already running on your PC.

## Signing in: passkeys and step-up

- Sign-in uses passkeys (WebAuthn). There are no passwords.
- The first device is enrolled with a one-time code printed by `hearthkeep serve` (or `hearthkeep enroll-code`). The code is valid for 15 minutes, works once, and first-device codes stop working as soon as a passkey exists.
- More devices are added from an already signed-in device, which needs a passkey step-up before it will issue a 15-minute code.
- Sessions are an unguessable cookie (`HttpOnly`, `SameSite=Strict`); only a hash of it is stored. They expire after 14 idle days by default (`session_idle_days`).
- The last remaining device cannot be removed, so you cannot lock yourself out. Add a second device first, then remove the old one. Removing a device ends its sessions.
- A **step-up** is a fresh passkey check for one sensitive action. The resulting token lasts 60 seconds, is kept only in memory, and is tied to your session and to that one purpose (and approval, where relevant).
- Step-up is required to: approve an outward action, widen a tool's permission level, issue a code to add a device, add an allowed folder, save or replace the GitHub token, add a repo, and delete a chat, a file or a repo.
- The step-up purposes are `approval`, `settings`, `add_device`, `add_folder`, `github_token`, `add_repo` and `delete`. A token is bound to its purpose: an `approval` token cannot add a folder. For `delete` it is also bound to one target (`chat:<id>`, `file:<id>` or `repo:<id>`), so a token for one chat cannot delete another.
- Removing a folder, removing the GitHub token, removing or renaming a device and signing out other sessions need only a normal session.

## Permissions

Every tool call is classified into one of three action classes, and every tool has a level: **Allowed**, **Ask** or **Off**. The table is the exact output of `permissions.decide`.

| Action class | What it means | Allowed | Ask | Off |
|---|---|---|---|---|
| Look | Reads or looks something up | Runs | Asks you | Blocked |
| Change local | Changes something on this PC | Asks you the first time in a chat, then runs for that chat | Asks you every time | Blocked |
| Outward | Anything that leaves the PC | Asks you, with step-up | Asks you, with step-up | Blocked |

Other rules:

- Unknown tools default to Ask. Anything the engine does not recognise is denied (it fails closed).
- Tools that need a sandbox (Python, GitHub tests) are treated as Ask when Docker is not available.
- The level keys added in Phase 3 are `python` (Look, runs only in the sandbox, default Allowed; `read_pages` uses it too), `files` (Look, default Allowed) and `send` (Look, default Allowed).
- The level keys added in Phase 4 are `github` (reading, editing, installing dependencies and the outward GitHub actions; default Ask) and `github_tests` (running a repo's tests in the sandbox, shown as *GitHub tests*; default Ask). Outward GitHub actions need an approval with a passkey at every level, `Allowed` included.
- Lowering a level needs only a normal session. Raising one (Off to Ask, or Ask to Allowed) needs a step-up.
- The demo tools are `clock` (Look) and `note` (Change local, writes `<data>/workspace/output/notes.txt`); they exercise the Allowed and Ask paths end to end. `generate_image` is Look.

## Approvals

When a call needs your say-so, Hearthkeep creates an approval and the assistant waits. Approvals can be resolved only from a signed-in owner session, never by the model. Resolution is a single atomic database update, so two requests cannot both win. Approvals expire after 15 minutes by default (`approval_ttl_minutes`); an expired approval is treated as declined. An approval that needs step-up cannot be approved without one. No reply survives a restart: when the server starts, every pending approval is expired and any reply that was still running is ended with a note that it was interrupted.

## Web hardening

- Every response carries a strict `Content-Security-Policy`: scripts, connections and everything else come only from Hearthkeep's own origin, with two exceptions, inline styles and `data:`/`blob:` images; no other site may frame it (`frame-ancestors 'none'`). Responses also carry `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.
- A request whose `Host` header is not the `public_origin` host or a loopback name (`127.0.0.1`, `::1`, `localhost`, any port) gets a 400. This blocks DNS-rebinding pages from talking to the server under their own name.
- Every state-changing request (POST, PUT, PATCH, DELETE) must carry the header `X-Hearthkeep: 1` and an `Origin` that exactly matches `public_origin`; otherwise it gets a 403.
- Sign-in and enrollment endpoints are rate limited: 10 attempts per minute per client address, and 30 per minute across all clients. Behind `tailscale serve` every request looks local, so the global cap is the one that bounds guessing.
- Unexpected server errors are logged by type only, never with message or traceback, because those can contain private data.

## Images and photo uploads

- Generated images and uploaded photos are served only to a signed-in owner session, with `X-Content-Type-Options: nosniff` and a private cache header. A request without a session gets nothing.
- Uploads are checked by content, not by name: the type is read from the file's magic bytes and its size from the parsed headers (PNG, JPEG or WebP only). The server stops reading once an upload is over the limit (15 MB), so an oversized request is dropped early. Photos over 4096 pixels on a side are refused, and animated files (animated PNG or WebP) are rejected.
- The client's file name is never stored or used. The server picks the id and builds the path itself, and only ids of the exact shape it hands out are accepted.
- The model can only refer to an upload by its id, and the id is looked up in the database. It cannot name a path or a file.
- Generating an image and uploading a photo are **Look** actions: they stay on this PC and need no approval. Nothing is sent anywhere.
- Known Phase 2 limits: uploads do not expire, ComfyUI keeps its own copy of a photo you edit in its `input` folder, and it also keeps a copy of every generated image in its `output` folder (names starting with `hearthkeep`). Hearthkeep cleans neither folder. See [models.md](models.md).

## Chat files

- Attached files are checked by content, not by name: magic bytes plus a parse check decide the type (CSV, `.xlsx`, `.xls`, PDF, text or JSON). A wrong extension changes nothing.
- At most 5 files per message and 50 MB per file; the server stops reading once a file is over the limit.
- Macros are refused: `.xlsm` files and any workbook that contains a macro project. A workbook whose entries would unpack to more than 1 GiB is refused. Formulas are read as the values stored in the file, and only inside the sandbox.
- A file that needs a password to open is refused with the message "This file is password-protected. Remove the password and attach it again." A PDF that opens without a password is accepted even if it has an owner password, as many bank statements do.
- Files live under `<data>/workspace/chats/<chat-id>/files/`, stored as `<file-id>.<ext>`. The server picks the ids and builds every path; only ids of the exact shape it hands out are accepted.
- The original file name is stored in the database and returned only to a signed-in session. It never appears in the audit log or the server logs: the audit log records the id, the type and the size. The local model sees names in the chat's file list.
- Deleting a chat or a single file needs a passkey step-up bound to that target. Deleting a chat removes its messages, events, approvals, grants, files, runs and their outputs, number checks, images, and the uploads and sent items that belong to it, and then its folders on disk. A file that cannot be removed straight away (locked, say) is removed by the sweep at the next start. Links and junctions met on the way are removed themselves, never followed. The audit log is not touched.
- Deleting a file keeps the tables and charts of runs that already used it; delete the chat to remove those too.

## The Python sandbox

Python runs only in Docker. Each run is one new container from the image `hearthkeep-sandbox:<hash>`, started with:

| Setting | Value |
|---|---|
| Network | `--network none`: no network at all |
| Root filesystem | `--read-only` |
| User | `--user 10001:10001`, not root |
| Capabilities | `--cap-drop ALL` |
| Privileges | `--security-opt no-new-privileges` |
| Memory | `--memory 4g --memory-swap 4g` (no swap) |
| CPUs | `--cpus 2` |
| Processes | `--pids-limit 256` |
| `/tmp` | tmpfs, `noexec`, 256 MB |
| `/out` | tmpfs, 256 MB: where results and files are written |
| `/data` | the chat's `files/` folder, mounted read-only |
| `/job` | the job folder (the script), mounted read-only |
| Lifetime | removed when it ends (`--rm`); stopped at 120 s (the script itself gets 110 s) |

What it never gets: a GPU, the Docker socket, the data folder, the secret store, your allowed folders, or any other chat's files. Printed output is cut at 256 KB with a note; the output archive is limited to 300 MB and 200 members. The image's Python libraries are pinned with hashes, and the image is built only when you run `hearthkeep sandbox build --data <data>`: it is never built or downloaded in the background.

Without Docker, the `python` level is forced to Ask, and Hearthkeep still never runs code on the host: an approved call without Docker returns a plain message that it needs Docker.

Every run is saved (the attach-time check of a file is not a run): the script, the printed output, the results, any files it made, its status and how long it took. Charts are Vega-Lite JSON that the sandbox emits and the server validates before storing (size and depth limits, inline data only, no URLs, no external references, no images). They are drawn offline from the app's own bundle, with a loader that refuses every load, so a chart cannot fetch anything.

Job folders are under `<data>/runtime/sandbox/jobs/<run-id>/` and are removed after each run.

## Checking the numbers

In a chat with files, every assistant reply is checked. Each number (dates and counts included) is marked:

- **computed**: it matches a value the analysis reported with hk.result in a run during this reply; a number repeated from an earlier reply without a new run shows as unverified;
- **read from the document**: it matches only a value in the document, and is labelled `read from page N, not computed`;
- **unverified**: it matches nothing, and is marked with a note.

Numbers in your own question and in the chat's file names are not marked. This is a check against what the runs and documents contain, not proof that an analysis is right: read the script when it matters.

## Allowed folders

- A folder is added from Settings and needs a passkey step-up (`add_folder`). Hearthkeep stores its real path and checks it again on every use; a folder that has moved or changed is refused until you remove it and add it again.
- Links are never followed: symlinks, junctions, mount points and other reparse points are refused at every depth. OneDrive files that are on this PC are read like ordinary files; online-only files are skipped (the assistant is told how many) and never downloaded. Hidden and system files, names starting with `.` or `~$`, and files with more than one hard link are skipped.
- Hearthkeep's own data folder, its repository, AppData, Windows, Program Files and ProgramData are never listed or read, even when an allowed folder contains them. Network and device paths, alternate data streams, reserved device names and 8.3 short names are refused.
- Files are opened without following links, and the open handle's final path is compared with the path that was checked, so a swap between the check and the open is caught.
- Hearthkeep never writes to, moves or deletes anything in an allowed folder. To work on a file, the assistant copies it into the chat's `files/` folder.
- Folder paths are not written to the audit log or the server logs, only ids and labels.

## Notifications

When a file or summary is sent to your devices, Hearthkeep can nudge a device that opted in with a web push notification. This is the only notification connection, and it carries no content. (GitHub, once you turn it on, is the other outgoing connection: see [GitHub](#github).)

- **No content.** The push has no body at all. The notification text is fixed in the app (`A file is ready in Hearthkeep`); the file itself is fetched from your own PC when you open the app.
- **What the push service sees.** Google, Mozilla, Apple or Microsoft sees the device's endpoint address and a VAPID signature proving the push comes from your server. They never see a file, a name, a chat or a summary.
- **Allow-list.** Hearthkeep sends only to HTTPS endpoints (port 443) on `fcm.googleapis.com`, `updates.push.services.mozilla.com`, `web.push.apple.com` and `*.notify.windows.com`. An endpoint on any other host is rejected when it is saved.
- **Strict client.** `trust_env=False` (no system proxy), no redirects followed, a 10 second timeout, one POST per device, with `TTL: 86400` and `Urgency: normal`.
- **Keys.** The VAPID private key is created on your PC and kept in the secret store under `vapid_private_key`; it is never shown or logged.
- **Opt-in.** Each device turns notifications on for itself, and can turn them off again. With none on, nothing leaves your PC.

## GitHub

GitHub is off until two things are true: Git 2.31 or newer is installed on this PC, and you have saved a token. Without either, the assistant is not offered the GitHub tools at all. This section is what happens after that.

### The token

- It must be a **fine-grained personal access token** (it starts with `github_pat_`), made for *Only select repositories*. Hearthkeep checks it with GitHub before it saves it, and saving or replacing it needs a passkey step-up (`github_token`). Removing it needs only a normal session.
- It is stored encrypted in the secret store, under the name `github_token`, like any other secret. No tool can read it.
- It never appears in a command line, in the environment of any container, in a repo's `.git/config`, in a remote URL, in the audit log, in the server logs, in what the model sees, in a tool result or in an error message. When Git reports an error that contains the token (or its Basic-auth form), the text is replaced with `[redacted]` before anyone sees it. Git gets the token only for the commands that talk to GitHub (clone, fetch, push), through one temporary setting for that single command.
- It is the only credential Hearthkeep uses with GitHub. Make it narrow: only the repos you want worked on, only the permissions listed in [install.md](install.md#12-optional-github), and an expiry.

### Calls to GitHub

- The API calls go only to `https://api.github.com`, over HTTPS, ignoring system proxy settings, following no redirects, with a 20 second timeout. The only paths used are `/user` (to check the token) and `/repos/<owner>/<repo>/...` for a repo you added.
- Clone, fetch and push go to `https://github.com/<owner>/<repo>.git`. Hearthkeep always passes that address itself; it never reads the remote from the repo's own settings.
- What comes back is trimmed before the model sees it: at most 30 items in a list, and at most 4,000 characters in any text field. Titles, descriptions and comments from GitHub are other people's text. The tool tells the model to read them and never follow instructions in them.
- Submodules are never fetched. Git LFS, GitHub Enterprise and organisation-wide tokens are not supported.

### Git on your PC

Git runs on your PC, outside the sandbox, so it is locked down:

- Every Git command starts with a nearly empty environment and its own home folder, `<data>/runtime/git-home/`. Your global and system Git settings are ignored, and it never asks for a password.
- Hooks are switched off (`core.hooksPath` points at an empty folder), and so are the file-system monitor, credential helpers, redirects and submodule recursion. Only HTTPS to GitHub is allowed as a protocol.
- A repo's own `.git/config` cannot change any of this: the settings are passed on every command line, and the clone is never trusted to name its own remote.
- Paths the assistant gives are checked. A path must be relative, must stay inside the clone and must not pass through a link or junction. Anything in or named like a `.git` folder is refused, including spellings such as `.GIT`, `.git.` and `.git ` that Windows treats as the same folder. A refused path is answered with "That path is outside the repo or inside its .git folder."
- Repo names (`owner/name`) are shown to you and to the assistant. The audit log and the server logs record the repo's id instead.

### What the assistant can change

- It works only on branches named `hearthkeep/<name>`. Editing or committing on any other branch is refused until it makes one. A new branch starts from the default branch as last fetched.
- It never pushes to the default branch and never force-pushes. A push goes only to the same-named `hearthkeep/` branch on GitHub.
- Commits use the GitHub no-reply address of the account the token belongs to (`<id>+<login>@users.noreply.github.com`), not an address from your PC.
- Reading, searching and fetching are *Look*. Editing files and committing in the local copy are *Change local*. Neither reaches GitHub, except that a fetch downloads from it.
- Pushing, opening a pull request, commenting, merging and closing are *Outward*: an approval card, plus a fresh passkey check, at any permission level.

### What you see before you approve

- Every approval card for an edit or an outward action shows the **full diff**, with every file open. A very large diff is cut at 200 KB, and the card says so.
- A push sends **exactly the commit shown on the card**. The card records that commit; if the branch moved after you approved, because the assistant edited or committed again, the push is refused with "The branch changed after you approved it. Nothing was pushed."
- A merge card shows the base branch, and it is checked again when the merge runs. A close card checks whether the number is an issue or a pull request. A card to open a pull request from a branch that has not been pushed says it will be refused.

### Running a repo's tests

A repo's tests are code you did not write, so they run only in Docker, in the image `hearthkeep-repo:<hash>`. The container is started like the Python sandbox (not root, all capabilities dropped, `no-new-privileges`, read-only root, no swap), with these settings:

| Setting | Value |
|---|---|
| Network | `--network none`: no network at all |
| Code | A **copy** of the working tree, without `.git`. The copy on your PC is mounted **read-only** at `/src`; the container first copies it into its own copy at `/repo`, which Docker removes with the container. A copy over 500 MB or 50,000 files is refused |
| Dependencies | The cached volume, mounted read-only |
| Environment | `HOME=/tmp` and `CI=1`; no token, no secrets |
| Memory, CPUs, processes | 4 GB with no swap, 2 CPUs, 512 processes |
| Time | Stopped at 10 minutes and removed |

Because the tests run on a copy without `.git`, a test that writes into `.git` or plants a hook cannot reach your clone. The copy on your PC is read-only to the container, so a test cannot change it either: what a test writes goes into the container's own copy, which Docker removes with the container. On Linux the copy on your PC is only readable by the container's user (files 0644, folders 0755, never group- or world-writable) and sits in a job folder only you can open (0700). Output is kept as the first 64 KB and the last 192 KB with a note between them. The model sees at most 8,000 characters of a run's output; the full output is in the Repos view. Every run is saved, including runs that were stopped or timed out. Without Docker, `GitHub tests` is forced to Ask, and an approved call answers that it needs Docker: Hearthkeep never runs a repo's code on the PC itself.

### Installing dependencies

Tests need the repo's packages, and installing them needs the internet, so it is a separate step with its own rules:

- Only three kinds of project are supported: `uv` (`uv.lock`), `pip` (`requirements.txt`, and `requirements-dev.txt` if present) and `npm` (`package-lock.json`).
- An install that is not cached yet is **Outward**: it needs your approval with a passkey, at any level, and it runs only after that. The card shows the command and the lock file's hash.
- The install container has Docker's default network. That is **full network access, like any program on your PC, including your home network** (your router, printers, other computers, and Hearthkeep's own PC). It sees **only the dependency files**, copied in read-only, never the clone, and **never your token or your other files**. It writes only to its own volume, and it is stopped at 15 minutes. If Hearthkeep stops in the middle of an install, the next start stops that container and removes its half-filled volume.
- Dependencies are installed **once per lock file**. They are kept in a Docker volume named `hk-deps-<repo id>-<hash>`, and the hash covers the dependency files and the image. Change a lock file and the next install asks again; unchanged, `repo_install` is a *Look* and runs without asking. Older volumes of that repo are removed after a successful install.
- The model sees at most 8,000 characters of install output.

### Removing a repo

Removing a repo deletes its clone, and any work in it that was not pushed. It needs a passkey step-up bound to that repo (`delete` with `repo:<id>`). If something cannot be deleted straight away, the sweep at the next start finishes it.

## Audit log

Security-relevant events (for example enrollment codes, sessions, step-ups, device changes, permission changes and approvals) are recorded in the database with a time and an actor. Values under keys such as `token`, `secret`, `api_key`, `password` and `code` are redacted before they are written. The HTTP API can only read the log; it has no way to edit or delete entries.

## Secrets

Secrets (for example an API key for your local chat server) are encrypted with a Fernet key held in your operating system's keyring; the encrypted values live in `<data>/secrets/`. Manage them from the command line:

- `hearthkeep secrets set NAME --data <data>` reads the value from a hidden prompt (or from standard input), so it never appears in your shell history.
- `hearthkeep secrets list --data <data>` prints names only, never values.

Keep secrets out of `config.toml`; it holds only the name of the secret to use (`chat_api_key_secret`).

## Data folder layout

`hearthkeep serve --data <data>` creates this on first run:

| Path | Contents |
|---|---|
| `<data>/config.toml` | Your settings, copied from `config.example.toml` |
| `<data>/hearthkeep.db` | Chats, approvals, audit log, devices, permission levels |
| `<data>/secrets/` | Encrypted secret store |
| `<data>/workspace/output/` | Files the assistant produces |
| `<data>/workspace/output/images/` | Images the assistant generated |
| `<data>/workspace/uploads/` | Photos you uploaded |
| `<data>/workspace/chats/<chat-id>/files/` | Files attached to that chat, or copied in from an allowed folder |
| `<data>/workspace/chats/<chat-id>/runs/<run-id>/` | The outputs of that chat's analysis runs |
| `<data>/workspace/repos/<repo-id>/` | One clone for each repo you added in Repos |
| `<data>/runtime/sandbox/jobs/` | Temporary job folders for the sandbox, removed after each run |
| `<data>/runtime/repo-jobs/` | Throwaway copies of a repo's working tree for its test runs, read-only to the container and removed after each run |
| `<data>/runtime/deps/` | The dependency files copied for an install, removed after each install |
| `<data>/runtime/clone-<id>/` | A repo being cloned while you add it, moved into `workspace/repos/` when done |
| `<data>/runtime/git-home/` | The home folder every Git command uses: an empty config and an empty hooks folder |
| `<data>/logs/`, `<data>/private/` | Logs (the model servers' output, and `hearthkeep.log` when started at login) and private files |

None of this is ever part of the repository. Back it up yourself: [install.md](install.md#15-backing-up) lists what matters and how the keyring key affects a restore.

## What the assistant can never do

- Send anything off your PC, or take an outward action, without your approval and a fresh passkey check. The one exception is the payload-free push notification, which carries no content.
- Approve its own requests, or change its own permissions. Both require your signed-in session.
- Raise a tool's permission level.
- Talk to a model server or listen on any address outside your own machine.
- Read your secrets through any tool. No tool touches the secret store, and `secrets list` prints names only.
- Edit or erase the audit log.
- Change Hearthkeep's own code or configuration.
- Write to, move or delete anything in your allowed folders, or reach a folder you have not allowed.
- Run code on your PC itself: Python and a repo's tests run only in the sandboxes.
- Push to a repo's default branch, force-push, or push anything but the commit you approved.
- Read the GitHub token through any tool, or reach a repo you did not add.

## Development-only switch

`hearthkeep serve --insecure-dev-auth` accepts any passkey response so the app can be tested without real hardware. It refuses to run unless the server is on a localhost origin and the data folder holds no real passkey (a separate development data folder is the safe choice), and it prints a warning. Never use it for real data.

## Reporting a vulnerability

Please report privately, not in a public issue. See [SECURITY.md](../SECURITY.md) for how.
