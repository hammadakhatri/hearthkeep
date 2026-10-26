# Hearthkeep security model (Phase 1)

This page describes what the code in this repository does today. Phase 1 is the secure core: sign-in, permissions, approvals, audit and secrets, plus two demo tools. The six real tools (budget, news and search, files, Python, images, GitHub) arrive in later phases and are not part of this release.

`<data>` below means your own Hearthkeep data folder.

## Network model

- The server binds to `127.0.0.1` only. A `host` in `config.toml` that is not a loopback address is rejected at startup.
- The chat model must also be local: `chat_base_url` has to point at `127.0.0.1`, `::1` or `localhost`, otherwise the server refuses to start. Hearthkeep only talks to local models.
- Remote access is meant to go through your private Tailscale network (`tailscale serve` in front of the loopback port). Nothing is exposed to the public internet. If `public_origin` is not localhost it must be `https`, and the session cookie is then marked `Secure`.
- The server ignores `X-Forwarded-For`, so a local process cannot fake its address to dodge rate limits.

## Signing in: passkeys and step-up

- Sign-in uses passkeys (WebAuthn). There are no passwords.
- The first device is enrolled with a one-time code printed by `hearthkeep serve` (or `hearthkeep enroll-code`). The code is valid for 15 minutes, works once, and first-device codes stop working as soon as a passkey exists.
- More devices are added from an already signed-in device, which needs a passkey step-up before it will issue a 15-minute code.
- Sessions are an unguessable cookie (`HttpOnly`, `SameSite=Strict`); only a hash of it is stored. They expire after 14 idle days by default (`session_idle_days`).
- The last remaining device cannot be removed, so you cannot lock yourself out. Add a second device first, then remove the old one. Removing a device ends its sessions.
- A **step-up** is a fresh passkey check for one sensitive action. The resulting token lasts 60 seconds, is kept only in memory, and is tied to your session and to that one purpose (and approval, where relevant).
- Step-up is required to: approve an outward action, widen a tool's permission level, and issue a code to add a device.

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
- Lowering a level needs only a normal session. Raising one (Off to Ask, or Ask to Allowed) needs a step-up.
- Phase 1 ships only demo tools: `clock` (Look) and `note` (Change local, writes `<data>/workspace/output/notes.txt`). They exist to exercise the Allowed and Ask paths end to end.

## Approvals

When a call needs your say-so, Hearthkeep creates an approval and the assistant waits. Approvals can be resolved only from a signed-in owner session, never by the model. Resolution is a single atomic database update, so two requests cannot both win. Approvals expire after 15 minutes by default (`approval_ttl_minutes`); an expired approval is treated as declined. An approval that needs step-up cannot be approved without one.

## Web hardening

- Every response carries a strict `Content-Security-Policy` (own origin only, no framing), `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.
- Every state-changing request (POST, PUT, PATCH, DELETE) must carry the header `X-Hearthkeep: 1` and an `Origin` that exactly matches `public_origin`; otherwise it gets a 403.
- Sign-in and enrollment endpoints are rate limited: 10 attempts per minute per client address, and 30 per minute across all clients. Behind `tailscale serve` every request looks local, so the global cap is the one that bounds guessing.
- Unexpected server errors are logged by type only, never with message or traceback, because those can contain private data.

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
| `<data>/workspace/repos/` | Reserved for later phases |
| `<data>/logs/`, `<data>/private/` | Logs and private files |

None of this is ever part of the repository. Back it up yourself.

## What the assistant can never do

- Send anything off your PC, or take an outward action, without your approval and a fresh passkey check.
- Approve its own requests, or change its own permissions. Both require your signed-in session.
- Raise a tool's permission level.
- Talk to a model server or listen on any address outside your own machine.
- Read your secrets through any tool: Phase 1 has no tool that touches the secret store, and `secrets list` prints names only.
- Edit or erase the audit log.
- Change Hearthkeep's own code or configuration.

## Development-only switch

`hearthkeep serve --insecure-dev-auth` accepts any passkey response so the app can be tested without real hardware. It refuses to run unless the server is on a localhost origin and the data folder holds no real passkey (a separate development data folder is the safe choice), and it prints a warning. Never use it for real data.

## Reporting a vulnerability

Please report privately, not in a public issue. See [SECURITY.md](../SECURITY.md) for how.
