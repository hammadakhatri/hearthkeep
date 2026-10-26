# Hearthkeep

**Your private AI, at home.**

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Hearthkeep is an open-source, local-first personal assistant. It runs on your own PC, thinks with local models only, and is reachable from your other devices over your private Tailscale network. Anything that leaves your PC or changes something permanently waits for your explicit approval.

**Status:** Phase 2: models and images. On top of the Phase 1 secure core (passkey sign-in, permissions and approvals, audit log, encrypted secrets, chat UI), Hearthkeep can now run its own models: model cards, a process supervisor and a GPU scheduler that swaps between the chat model (llama.cpp) and image models (ComfyUI), a `generate_image` tool, an Images view and models list, and photo uploads with FLUX.2 klein editing. The other tools (budget, news and search, files, Python, GitHub) arrive in later phases; `clock` and `note` are still demo tools.

## Run Hearthkeep

You need Python 3.12+, [uv](https://docs.astral.sh/uv/) and Node 20.19+ (or 22.12+). For the models, either point Hearthkeep at an OpenAI-compatible chat server already running on your own machine (by default `http://127.0.0.1:8080/v1`), or let Hearthkeep start the models itself. The second way needs llama.cpp (chat) and/or ComfyUI (images) installed, and an NVIDIA GPU so Hearthkeep can detect its memory. See [docs/models.md](docs/models.md).

```
# build the web app (the server serves web/dist)
cd web
npm ci && npm run build

# install and start the server; <data> is your own data folder
cd ../server
uv sync
uv run hearthkeep serve --data <data>
```

On first run the data folder is created with a `config.toml` you can edit (model name, chat server address, port). If you change `port`, change `public_origin` to match (and `rp_id` if the host name changes). If your chat server needs a key, store it with `uv run hearthkeep secrets set chat_api_key --data <data>`.

To let Hearthkeep run the models itself, copy `models.example.toml` to `<data>/models.toml` and fill in the paths for your machine: with `llama_server_exe` and a `model_dirs` entry for the chat model set, Hearthkeep starts llama-server on first use (with its own generated key) and stops it when Hearthkeep stops; with the three `comfyui_*` paths set, it starts ComfyUI for image generation the same way. Without `models.toml`, Hearthkeep uses the chat server already running at `chat_base_url` and image generation stays off. On start the server prints the model cards it loaded and any card problems. Cards, `models.toml`, GPU switching, photo uploads and troubleshooting are covered in [docs/models.md](docs/models.md).

The server prints a one-time enrollment code. Open `http://localhost:8765`, enter the code within 15 minutes and create your first passkey. Lost the code? Run `uv run hearthkeep enroll-code --data <data>`. Add a second device before removing the first: the last device cannot be removed.

`--insecure-dev-auth` is for development only; see the security notes before using it.

## Learn more

- [docs/design.md](docs/design.md): design
- [docs/models.md](docs/models.md): model cards, `models.toml`, GPU switching and images
- [docs/security.md](docs/security.md): security model, permissions and what the assistant can never do
- [SECURITY.md](SECURITY.md): how to report a vulnerability
- [CONTRIBUTING.md](CONTRIBUTING.md): how to contribute

## License

Apache 2.0, see [LICENSE](LICENSE).
