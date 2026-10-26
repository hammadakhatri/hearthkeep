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
