# Contributing to Hearthkeep

## The one rule: nothing personal in the repository

The repository holds code, design and documentation only. It must never contain a real path, name, account, repository name, token, chat or image belonging to any user. Personal values live in the data folder (`<data>`), which is outside the repo, and in gitignored files.

Use sample data in tests, docs and screenshots.

## Privacy scanner

`scripts/privacy_scan.py` flags absolute user paths, links to private claude.ai artifacts, tokens and private keys, plus any identifiers you list one per line in `.hearthkeep-personal-markers` at the repo root (UTF-8 or UTF-16, `#` starts a comment). That file is gitignored: it is your own local list and is never committed. If the file exists but cannot be decoded or lists no markers, the scanner warns, and the hooks refuse the commit until you fix or delete it.

```
python scripts/privacy_scan.py path/to/file ...
python scripts/privacy_scan.py --staged           # scan what is staged for commit
git log -p | python scripts/privacy_scan.py -     # '-' reads standard input
```

It exits 1 on any finding. CI runs it over the whole tree and also runs gitleaks.

## Hooks

Install the plain-shell hooks (works in Git Bash on Windows, needs `python` on PATH):

```
git config core.hooksPath .githooks
```

If you prefer the pre-commit framework, `.pre-commit-config.yaml` defines the same two checks.

## Commit messages

Use conventional commits (`feat:`, `fix:`, `docs:`, `chore:`, `test:`).

**Commit messages must not contain a `Co-Authored-By` trailer or an AI attribution line** (for example "Generated with" followed by an AI tool name). The `commit-msg` hook rejects them, and CI checks every commit message in a push or pull request with `scripts/check_commit_msg.py`. The same check also rejects messages that contain absolute user paths, private links, tokens or one of your personal markers.

## Tests

```
cd server
uv run pytest
uv run ruff check . ../scripts
```

## Releasing

Owners install Hearthkeep from a GitHub release, which carries the prebuilt web app. `main` is for developers and carries a `0.0.0` or `.devN` version; only an `x.y.z` version (not `0.0.0`) is a release.

1. Set the version in both `server/pyproject.toml` and `server/src/hearthkeep/__init__.py` (a test keeps them equal), for example `0.1.0`, and commit.
2. Tag that commit `vX.Y.Z` (for example `v0.1.0`) and push the tag.
3. The `Release` workflow (`.github/workflows/release.yml`) builds and tests the web app, runs `scripts/build_release.py` (it fails when the tag and the code's version differ), and makes a **draft** release with four assets: the Hearthkeep zip (the tag's source plus the prebuilt `web/dist` and its manifest), the web-app zip on its own (for people who cloned the tag; setup downloads it from the release of the clone's `origin` remote), and a `.sha256` beside each.
4. Write the release notes on the draft, then publish it.
5. On `main`, set the version to the next development version, `X.Y.(Z+1).dev0` (for example `0.1.1.dev0`), in both files.

To try the assets locally, run `npm run build` in `web`, set a release version as in step 1, then run from the repository root (`.cache/` is gitignored):

```
uv run --project server python scripts/build_release.py --version X.Y.Z --out .cache/release
```
