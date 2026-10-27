# Hearthkeep documentation

| Document | What it covers |
|---|---|
| [install.md](install.md) | Installing Hearthkeep step by step: prerequisites, models, Tailscale, first passkey, running at login, the Python sandbox (Docker), phone notifications, updating, backups, troubleshooting, uninstalling. Start here. |
| [models.md](models.md) | Model cards, `models.toml`, GPU switching, vision for scanned pages, photo uploads and model troubleshooting. |
| [security.md](security.md) | What the code does today to keep you safe: network, passkeys, permissions, approvals, chat files, the Python sandbox, allowed folders, push notifications, secrets, audit log, data folder layout. |
| [design.md](design.md) | The design: intent, goals, architecture and the plan for every tool. |
| [superpowers/plans/](superpowers/plans/) | The implementation plans each phase was built from, one file per phase. |

Repository-level documents:

- [../README.md](../README.md): what Hearthkeep is and the roadmap
- [../CONTRIBUTING.md](../CONTRIBUTING.md): how to contribute, the privacy scanner and commit rules
- [../SECURITY.md](../SECURITY.md): how to report a vulnerability

`<data>` in every document means your own Hearthkeep data folder.
