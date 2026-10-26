# Hearthkeep

**Your private AI, at home.**

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![CI](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

Hearthkeep is a personal AI assistant for one owner. It runs on your own PC, uses local models only, and you reach it from your phone or Mac over your private Tailscale network. No AI provider ever sees your chats. Anything that would leave your PC or change something for good waits for your approval, confirmed with a passkey.

Hearthkeep is early software. It is for you if you have a Windows PC with an NVIDIA GPU, are comfortable with a terminal, and want an assistant that keeps your data at home.

## What it does today

- **Chat with local models.** Gemma 4 12B through llama.cpp, with streaming replies and tool calls shown step by step. Tool calls that need your say-so appear as approval cards.
- **Passkey sign-in.** No passwords: Face ID, Touch ID, Windows Hello or a PIN.
- **Permissions and approvals.** Every tool is Allowed, Ask or Off. Outward actions always need an approval plus a fresh passkey check.
- **Audit log.** Sign-ins, approvals, permission and device changes are recorded. The app can read the log but has no way to edit or delete it.
- **Encrypted secrets.** API keys are encrypted with a key held in your operating system's keyring.
- **Model cards and GPU switching.** Models are described by small TOML files. Hearthkeep starts llama-server and ComfyUI itself and swaps them on the GPU when they don't fit together.
- **Image generation.** Z-Image Turbo (good with words in the image) and FLUX.2 klein 4B (fast, photoreal), through ComfyUI.
- **Edit your own photo.** Attach a photo and ask for a change; FLUX.2 klein edits it.
- **Images gallery.** Every image you made, in one view.
- **Access from your phone or Mac** over Tailscale, with HTTPS from `tailscale serve`.

Budget, news and search, files, Python and GitHub tools are planned (see the roadmap). The `clock` and `note` tools are demo tools.

## Privacy by design

- Everything listens on `127.0.0.1` only. Hearthkeep refuses to start with any other address, and refuses a chat server that is not on your own machine.
- Remote access goes only through your private Tailscale network. Never Tailscale Funnel, never port forwarding.
- Local models only. There is no cloud AI fallback.
- Your data lives in a data folder you choose, outside the repository. The repository never contains anything personal, and a privacy scanner checks every commit.
- The model cannot approve its own requests, change permissions, or read your secrets.

## Requirements

| | Needed |
|---|---|
| OS | Windows 11 (the tested host). macOS and Linux work as clients; as a host they are untested. |
| GPU | NVIDIA, about 16 GB VRAM for the shipped models. |
| RAM | 32 GB recommended. |
| Disk | About 60 GB for the shipped models and tools. |
| Software | Git, Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 20.19+ or 22.12+, [Tailscale](https://tailscale.com) for remote access. |

You can also skip the GPU part and point Hearthkeep at an OpenAI-compatible chat server you already run on the same PC. Image generation is then off.

## Quick start

The full step-by-step guide, including model downloads and Tailscale, is in **[docs/install.md](docs/install.md)**. In short:

```
git clone <repository URL> hearthkeep
cd hearthkeep/web
npm ci
npm run build
cd ../server
uv sync
uv run hearthkeep serve --data <data>
```

`<data>` is a folder of your choice outside the repository. Open `http://localhost:8765`, enter the enrollment code the server printed, and create your passkey. Decide on remote access **before** you create it: a passkey made on `localhost` does not work on your Tailscale address. See [Decide on remote access](docs/install.md#7-decide-on-remote-access-before-your-first-passkey).

## Roadmap

| Phase | Delivers | Status |
|---|---|---|
| 1. Secure core | Passkeys, permissions and approvals, audit log, secrets, chat | Done |
| 2. Models and images | Model cards, GPU switching, image generation, photo editing, Images view | Done |
| 3. Files and Python | Files tool, send a file to your device, Python in a Docker sandbox, Analysis view | Planned |
| 4. GitHub | Your repos, sandboxed tests, approved pushes and pull requests, Repos view | Planned |
| 5. Briefing | Budgeting app (read only), RSS news, web search through SearXNG, daily briefing, memory, Today view | Planned |
| 6. Release | One-command installer, run at startup, audit log view, command palette, security review, v0.1 | Planned |

The design behind these phases is in [docs/design.md](docs/design.md). Each phase is built from a written plan in [docs/superpowers/plans/](docs/superpowers/plans/).

## Documentation

- [docs/install.md](docs/install.md): installation, Tailscale, running at login, updates, backups, troubleshooting
- [docs/models.md](docs/models.md): model cards, `models.toml`, GPU switching and images
- [docs/security.md](docs/security.md): the security model and what the assistant can never do
- [docs/design.md](docs/design.md): the design
- [docs/README.md](docs/README.md): index of all docs

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first: the one hard rule is that nothing personal ever goes into the repository.

## Reporting a security issue

Please report vulnerabilities privately, not in a public issue. See [SECURITY.md](SECURITY.md).

## License

Apache 2.0. See [LICENSE](LICENSE).
