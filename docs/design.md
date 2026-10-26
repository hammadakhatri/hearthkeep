# Hearthkeep: design

Status: draft
License: Apache 2.0

This is the public design. All examples use generic or sample values; real values come from each user's own data folder, written `<data>` below.

---

## 1. Intent

Hearthkeep is a private, always-on personal assistant that runs on one person's own PC and is reached from their other devices (laptop, phone) anywhere in the world. It thinks with **local models only**. No AI provider ever sees the owner's data, so nothing can be used for training or leak through a third party.

It is "an assistant for everything" for one person: a morning briefing (money, news, tech), answers and analysis, files on demand, image creation for products, banners and posts, and hands-on help with the owner's private GitHub repos, all with the owner's explicit approval before anything leaves the PC or changes permanently.

It is open source so other people can run the same thing on their own hardware. The repository holds code, design, rules and documentation, and **never** anything personal to any user.

### Why

- One place that knows your world (accounts, files, repos, interests) without handing that world to a cloud AI.
- Local models are now good enough for briefing, analysis, tool use and image generation on a single consumer GPU.
- Existing tools (Open WebUI, ComfyUI, llama.cpp's chat page) each cover a slice; none gives a calm, premium, single-owner assistant with approvals designed in from the start.

### What success looks like

- The owner opens Hearthkeep on their phone in another city, sees today's balance and news, approves a repo change with Face ID, and gets a file from their PC, all within a minute and without thinking about how.
- Someone else can follow the installation guide on a fresh Windows + NVIDIA PC and have it working the same day.
- An audit of the repo finds nothing personal, ever.

## 2. Roles

- **The maintainers** design and build Hearthkeep. Changes to Hearthkeep itself only come through its maintainers and contributors.
- **The local model** is the worker *inside* Hearthkeep: it answers, fetches data, analyses, writes image prompts and works on the owner's private repos. It **never modifies Hearthkeep's own code or configuration**.
- **Maintainers never touch a user's private repos.** Only the local model does, and only through Hearthkeep's approval flow.

## 3. Goals, non-goals, anti-goals

### Goals (version 1)
1. Single owner per installation; secure remote access from the owner's devices.
2. Six tools: budgeting app (read), news and web search, files (find and send to device), Python, image generation, GitHub.
3. Plug-and-play models: chat and image models are described by data ("model cards") and can be added without code changes.
4. Approvals designed in: every action has a permission level, and outward or permanent actions always require the owner's explicit approval with a device biometric.
5. A premium, always-dark interface that feels instant on desktop and phone.
6. A public, Apache-2.0 repository with a complete installation guide.

### Non-goals (version 1)
- Multiple users or shared accounts.
- Any cloud AI model (no hosted AI provider APIs), even as a fallback.
- Hearthkeep editing its own code ("self-improving").
- Native mobile apps (the phone experience is an installable web app).
- macOS or Linux as the *host* (they work as clients; host support is designed for but not built).
- Image editing beyond what the shipped image workflows do; exact-text overlay on images (later).
- Push notifications (later).
- Background work on its own initiative (e.g. overnight repo checks). In v1 Hearthkeep only works when the owner asks, plus the scheduled morning briefing. A request the owner started keeps running if the browser tab closes, and shows up under "Needs you" when it finishes or needs approval. (To be revisited later.)
- Cloud file storage connectors (later; the design leaves a connector slot).

### Anti-goals (failure even if everything "works")
- Any personal data, path, account name, repo name or secret in the public repository.
- Any service reachable from the internet or the local network, other than through the owner's private Tailscale network.
- Any outward or permanent action taken without an explicit approval the owner can see.
- The local model being able to change Hearthkeep, its permissions or its approval rules.
- A UI that feels like a dense admin dashboard instead of a calm product.

## 4. Constraints

- **Reference host:** Windows 11 + 16 GB NVIDIA GPU. Version 1 must run well here; other NVIDIA GPUs with 12 GB+ VRAM should work with smaller models.
- **GPU budget:** the chat model (Gemma 4 12B, ~11 GB VRAM) and an image model (~10 GB) cannot be loaded at the same time on 16 GB. Hearthkeep must schedule the GPU (section 7).
- **Existing installs:** a user may already have llama.cpp's `llama-server` (CUDA build), ComfyUI with image models, and a shared text encoder. The installer must be able to *use* existing installs as well as install fresh.
- **Everything binds to 127.0.0.1.** Remote access only via Tailscale (`tailscale serve`).
- **Open source:** Apache 2.0. Models are never committed; they are downloaded by the user with checksum verification. llama.cpp and ComfyUI are separate programs called over their local HTTP APIs.

## 5. Decisions

These are settled for version 1.

- Name **Hearthkeep**; license **Apache 2.0**; public GitHub repo from day one; full installation guide in the repo.
- **Always dark** interface, inspired by Apple's website: premium, spacious, instant.
- Version 1 includes **all six tools**.
- The owner's own cloud accounts are acceptable data sources and destinations (e.g. their budgeting app, GitHub). AI providers are not.
- Private repos: the local model may change them **only after the owner approves each change**.
- Start with strict permissions and loosen them over time from a settings screen.
- Remote access from laptop and phone, including "send me that file from my PC" while away.
- Single owner, fully secured.

## 6. Architecture

```
Phone / laptop / PC browser
        │  HTTPS, Tailscale only (tailscale serve → 127.0.0.1)
        ▼
Hearthkeep server  (Python, 127.0.0.1)
  ├─ Web UI (static build, served by the server)
  ├─ Auth: passkeys + sessions + step-up approvals
  ├─ Agent: chat loop with tool calling, streaming
  ├─ Permission engine + approval queue + audit log
  ├─ Tools: budgeting · news/search · files · python · images · github
  ├─ Model switchboard: model cards → providers
  ├─ GPU scheduler + process supervisor (llama-server, ComfyUI)
  └─ Storage: data folder (SQLite, files, encrypted secrets)
        │
        ├─► llama-server   127.0.0.1 (OpenAI-compatible, API key known only to Hearthkeep)
        ├─► ComfyUI        127.0.0.1 (HTTP API)
        ├─► SearXNG        127.0.0.1 (Docker, optional)
        ├─► Python sandbox (Docker container per run, optional)
        └─► Owner's accounts: budgeting app MCP, GitHub API
```

### 6.1 Server
- **Python** (FastAPI or equivalent async framework), so the agent, Python tooling, MCP client and ComfyUI integration share one language.
- Binds only to `127.0.0.1` on a configurable port. Refuses to start if configured otherwise.
- Streams model output and tool progress to the UI (server-sent events or WebSocket).

### 6.2 Web UI
- **React + TypeScript**, built to static files served by the server; installable as a **PWA** (manifest + icons; no offline mode required in v1).
- One codebase for desktop and phone; phone layout uses bottom tabs.

### 6.3 Data folder (never in the repo)
A single configurable folder, default `<install root>/hearthkeep-data`, written `<data>` in docs. Contains:
- `hearthkeep.db` (SQLite): chats, messages, memories, approvals, audit log, settings, devices/passkeys.
- `secrets/`: encrypted tokens (section 9.4).
- `workspace/`: repo clones, Python scratch, `output/` for results and generated images.
- `logs/`.

The repo ships `config.example.toml` (or similar) with placeholders; the real config lives in the data folder.

## 7. Models

### 7.1 Model cards
Each model is a small data file (TOML). Generic cards for the supported models ship in the repo; users add their own in the data folder. A card states:
- `id`, display name, kind (`chat` | `image`), capabilities (`vision`, `tools`, `thinking`, `text-in-image`, `image-edit`).
- Provider and how to start it (llama-server arguments with path placeholders; ComfyUI workflow template file).
- VRAM estimate, context size, recommended sampling.
- For image models: **prompting guidance** the agent reads before writing a prompt (style, what it's good/bad at, supported sizes, steps), and selection hints (e.g. Z-Image-Turbo: prefer when the image contains text; FLUX.2 klein: prefer for text-free photos and for editing a supplied photo).

Version 1 ships cards for: Gemma 4 12B (chat), Z-Image-Turbo, FLUX.2 klein 4B distilled. Indicative figures on the reference host: Gemma ~73 tok/s; Z-Image ~8 s and FLUX ~3 s per 1536×864 image after warm-up; Z-Image rendered requested banner text correctly in 2 of 3 tries, FLUX in 0 of 3.

### 7.2 Providers
- **Chat:** any OpenAI-compatible endpoint (llama-server in v1). Hearthkeep can start/stop llama-server itself from the card.
- **Image:** ComfyUI HTTP API, with the workflow graph stored as a template in the card and filled with prompt/size/seed.

### 7.3 GPU scheduler
- Hearthkeep knows the GPU's VRAM and each card's estimate. Only one heavy model is resident at a time when they don't fit together.
- To render an image while chat is loaded: finish/pause the current chat stream, stop llama-server (or unload), run ComfyUI, then call ComfyUI's free-memory endpoint and restart llama-server. The UI shows this calmly ("Chat paused while the image renders").
- Requests queue rather than fail. If the owner sends a chat message during a render, it waits and says so.
- Hearthkeep supervises llama-server and ComfyUI as child processes (start, health check, restart on crash).

## 8. Agent and tools

### 8.1 Agent loop
- Tool calling via the chat model's native function calling (verified working with Gemma 4 12B through llama-server).
- Thinking on by default; per-chat toggle.
- Bounded steps per turn; every tool call and result is shown in a folded "Worked for N s · K steps" block.
- Tool results that are large (web pages, files) are trimmed/summarised before they enter the context (32K context in v1).

### 8.2 Permission engine
Every tool action is classified into one of three **action classes**:
- **Look**: reads and computes with no lasting change (read budgeting data, search, read files, read repos, generate an image into the workspace, Python in the sandbox).
- **Change local**: lasting change on the PC within Hearthkeep's areas (edit files in a repo clone, create a branch, write outside `workspace/output`).
- **Outward or permanent**: anything that leaves the PC or can't be undone (push, open/merge/close PRs or issues, delete files, send/upload anything, install packages, network access from Python).

The model has **no tool that changes Hearthkeep's settings**, permissions, model cards or code. The owner's own settings changes that **widen access** (raising a tool's level, adding allowed folders or repos, enrolling a device, adding a connector or token) require a passkey step-up and are audited, so a stolen session alone can't loosen anything. Narrowing access (lowering a level, removing a folder or device) needs only a signed-in session.

Each tool has an owner-set level, **Allowed / Ask / Off** (the Tools & permissions screen). Rules:
- *Off*: the tool isn't offered to the model.
- *Ask*: every call waits for approval.
- *Allowed*: **Look** actions run immediately; **Change local** actions still ask once per chat (the owner can approve "for this chat"); **Outward or permanent** actions **always** require an approval card plus a passkey step-up (Face ID / Touch ID / Windows Hello), regardless of level.
- The model cannot change permissions, approve its own requests, or see approval secrets. Approvals are created by the server and resolved only by an authenticated owner session with a fresh WebAuthn assertion.
- An approval shows exactly what will happen (diff, command, destination) and expires if not answered.

Version 1 defaults: budgeting, News & search, Files, Python, Images = **Allowed**; GitHub = **Ask**.

### 8.3 Tools (version 1)

**Budgeting app (read only)**
- Source: a budgeting app that offers an MCP server (e.g. Wallet by BudgetBakers). Hearthkeep connects as an MCP client with its own sign-in, read-only operations only (accounts/balances, records, budgets, categories).
- Used by the briefing (total balance, change since yesterday) and analysis (e.g. spending by category). Currency is whatever the user's accounts use.
- How a third-party MCP client authenticates to the app's MCP server must be verified before building this tool; see section 13.

**News and web search**
- Briefing news comes from **RSS/Atom feeds** grouped into sections; the shipped example config has example sections (such as *Local*, *World*, *Tech*) with example feeds; each user edits their own list. The local model writes short summaries with source and time.
- Web search via a self-hosted **SearXNG** in Docker on 127.0.0.1 (optional dependency; if absent, the search tool is off). A fetch tool turns a URL into readable text for the model.

**Files**
- The owner chooses allowed folders (e.g. Documents, Desktop, Downloads, the data folder). Everything else is invisible to the tool.
- Search by name, folder, type and date in v1 (content indexing later).
- **Send to device:** creates a one-time download link bound to the requesting signed-in session, valid 10 minutes; reachable only over Tailscale. Sending a file to the owner's own signed-in device is a *Look* action.

**Python**
- Runs in `workspace/` with its own Python environment, never the system Python.
- **With Docker available (preferred):** each run is a container with the allowed data folders mounted **read-only**, `workspace/` read-write, and **no network**. This is what *Allowed* means for Python.
- **Without Docker:** Python is forced to *Ask* (every script shown and approved), because the sandbox can't be enforced.
- Writing outside the workspace, installing packages and network access are *Outward or permanent* and always ask.
- Every script and its output are saved and shown (code block with "ran in your workspace · read-only · time").
- Budgeting data is provided to Python by Hearthkeep as files/structured input, not by giving Python network access.

**Image generation**
- The agent reads image model cards, picks a model (or uses the owner's choice), rewrites the request into that model's preferred prompt style, chooses size/steps, renders via ComfyUI, and shows the result large with variants and the exact prompt used (editable, re-runnable).
- Default batch: several variants (the interface draft shows 4).
- "Use my product photo" runs the FLUX.2 klein image-edit workflow with an uploaded photo.
- A per-owner **brand kit** (colours, logo file, preferred fonts as text in prompts) is stored in the data folder and offered to the prompt writer.
- Results go to `workspace/output/images`; saving elsewhere asks.

**GitHub**
- The owner adds repos explicitly; access via a **fine-grained personal access token** limited to those repos, stored encrypted.
- Repos are cloned into `workspace/repos/`. Reading, searching, running tests in the sandbox: *Look*.
- **Dependencies:** installing a repo's dependencies (npm, pip, etc.) is approved **once per repo**, and again only when its dependency/lock files change. The approval card shows the install command and lock file. Hearthkeep caches that environment (e.g. a per-repo container image or volume), so afterwards the model can edit, test and retry freely in the no-network sandbox; only outward actions ask.
- Running a repo's tests is running untrusted code: it counts as *Look* only inside the Docker sandbox. Without Docker, repo test runs are forced to *Ask*, like Python.
- Editing files in the clone and creating local branches: *Change local*.
- Pushing, opening/merging/closing PRs or issues, commenting: *Outward or permanent* (approval card with the full diff + passkey).
- Implementation via `git` plus the GitHub REST API or GitHub's official MCP server.

### 8.4 Briefing ("Today")
- Contents: greeting and date; total balance with change since yesterday and a small trend line; "Needs you" (pending approvals, finished long tasks); news sections; one "Worth building together" suggestion.
- Built ahead of time at a configurable hour (default early morning, when the GPU is idle) and refreshed on demand.

### 8.5 Memory
- A list of short facts about the owner (preferences, ongoing projects, brand details) shown in the UI, editable and deletable.
- The model may propose a memory; it's saved and visibly marked as new so the owner can remove it.

## 9. Security

### 9.1 Network
- All Hearthkeep services bind to `127.0.0.1`. Remote access only through `tailscale serve` (never Funnel). The installation guide explains this and refuses to document port forwarding.

### 9.2 Sign-in
- **Passkeys (WebAuthn)** only; no passwords.
- Passkeys are tied to one web origin, so Hearthkeep uses **one canonical HTTPS address**, the Tailscale `*.ts.net` name, on every device including the host PC.
- **First device:** enrolment is only possible from the host PC, using a one-time code printed by the server at first start.
- **More devices:** approved from an already signed-in device (one-time code or QR), and listed/revocable in settings.
- Sessions per device, with idle expiry.

### 9.3 Step-up approvals
- Outward or permanent actions require a fresh WebAuthn assertion at the moment of approval.

### 9.4 Secrets
- Tokens (GitHub, budgeting app, future connectors) and the internal llama-server API key are encrypted at rest using the OS key store (Windows DPAPI via a cross-platform keyring library).
- Secrets never enter model prompts or logs.

### 9.5 Audit
- Every tool call, approval, denial, sign-in and settings change is recorded in the audit log and viewable/searchable in the UI.

### 9.6 Prompt injection
- Content from web pages, files, repos and emails is data, not instructions. The permission engine is the real defence: injected text can at most cause a request that the owner sees on an approval card.

## 10. Interface

Key rules:

- **Always dark.** True black edges, soft grey surfaces, light text. One blue accent for actions; amber only for "needs you" / approvals; green for success; red for loss/errors.
- **Typography-led, spacious, one main thing per screen.** Large numbers for money; big images in the image view.
- **Instant and calm motion:** views appear immediately and settle with short, smooth transitions; replies stream word by word; long tasks show quiet progress, never blocking spinners. Respect reduced-motion settings.
- **Layout (desktop/laptop):** left sidebar (Today, Chat, Images, Analysis, Repos, Files; Tools & permissions at the bottom), a slim top bar with view title and a model/GPU status pill, a floating composer at the bottom with Think toggle and model picker.
- **Phone:** bottom tabs (Today, Chat, Images, Files, More), large approval cards, biometric prompt on approve.
- **Views:** Today; Chat (folded tool steps, approval cards with diff, file cards); Images (model choice with the reason, hero image, variants, editable prompt with specs); Analysis (answer, chart, Python code block with sandbox facts); Repos (list with status and pending changes); Files (search, recent, send to phone, allowed folders); Tools & permissions (per-tool Allowed / Ask / Off).
- **Customisable:** order and visibility of briefing cards and sidebar items, keyboard shortcuts, command palette (Ctrl/Cmd+K). Theme stays dark.

## 11. Open-source repository

### 11.1 Contents
- Code: server, web UI, tools, providers, model cards, ComfyUI workflow templates.
- `docs/`: this design, architecture overview, permission model, security model, how to add a model, how to add an MCP connector.
- `README.md`, `LICENSE` (Apache 2.0), `SECURITY.md`, `CONTRIBUTING.md`, `config.example.*`.

### 11.2 Nothing personal, enforced
- No real paths, names, accounts, repo names, tokens, chats or images. All personal values come from the data folder.
- `.gitignore` excludes data folders, configs, models, logs, outputs.
- A privacy scanner (`scripts/privacy_scan.py`) and a secret scanner (gitleaks) run as pre-commit hooks and in CI; CI fails on findings. The privacy scanner catches personal markers, not only tokens: absolute Windows/home paths, claude.ai artifact links, and a local, uncommitted list of the contributor's own identifiers (names, account and repo names).
- Commit messages carry no co-author trailers or AI attribution lines; a commit-msg hook and CI enforce it.
- Screenshots and demo content use sample data only.

### 11.3 Installation guide
Must include: requirements (OS, GPU/VRAM tiers, disk, RAM); a one-command PowerShell installer *and* the same steps manually; choosing and downloading models by GPU size with checksum verification and resumable/parallel downloads (Hugging Face can be slow); using existing llama.cpp/ComfyUI installs; first launch and passkey enrolment; Tailscale setup for laptop and phone; adding GitHub (fine-grained token), a budgeting app and other MCP connectors; Docker for the Python sandbox and SearXNG (optional, with what you lose without it); running at startup; updating; troubleshooting (VRAM, ports, slow downloads, Windows quoting issues).

## 12. Testing and quality
- The permission engine and approval flow get thorough automated tests (every action class × level, step-up required, model can't self-approve).
- Tools tested against fakes (fake MCP server, fake GitHub, fake ComfyUI, fake chat endpoint).
- An end-to-end smoke test runs against real local models on the reference host before each release.
- A security review before the first public tagged release (v0.1).

## 13. Open questions and verification needed

1. **Budgeting app MCP access:** confirm the MCP server URL and how a third-party MCP client signs in, and which tools are read-only. If a third-party client can't connect, fall back to the app's own API if one exists, and otherwise report back before building this tool.

## 14. Left to implementation
- Exact frameworks within the stated choices, folder layout, streaming transport, ports.
- Step limits, timeouts, session lengths, variant counts.
- Feed defaults for the example config.
- How the model switchboard and GPU scheduler are structured internally.
- Visual details beyond section 10.
