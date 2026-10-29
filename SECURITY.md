# Security policy

Hearthkeep guards personal data, so security reports are taken seriously.

## Reporting a vulnerability

Please report vulnerabilities **privately**. Do not open a public issue.

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**. Include what you found, how to reproduce it, and the impact you expect. You will get an acknowledgement, and we will work with you on a fix and a coordinated disclosure.

## Scope

In scope: authentication and approvals, the permission engine, the file and sandbox tools, secrets handling, and anything that could expose a service beyond `127.0.0.1` or the owner's private Tailscale network.

## How the repository is checked

Every commit and pull request goes through these checks; failing any of them fails the build.

| Check | What it catches |
|---|---|
| Privacy scan (pre-commit hook and CI) | Personal paths, names, accounts, tokens and private links |
| Commit message guard (hook and CI) | Co-author and AI attribution lines |
| [Gitleaks](https://github.com/gitleaks/gitleaks) | Passwords and API keys, across the whole history |
| Dependency audit (`pip-audit`, `npm audit`) | Known vulnerabilities in the server, the sandbox image and the web app |
| [CodeQL](https://codeql.github.com) (security-extended queries) | Injection, path traversal and similar bugs in the Python and TypeScript |
| [OpenSSF Scorecard](https://scorecard.dev) | Weak repository practices: unpinned actions, broad permissions, missing reviews |
| Tests | About 1,500 server and 230 web tests, many of them security tests: the sandbox, charts, push, folders, passkey step-ups |

On GitHub, secret scanning with push protection, Dependabot alerts and security updates, and private vulnerability reporting are on. `main` can't be force-pushed or deleted. Every GitHub Action is pinned to a full commit hash, workflows get read-only tokens unless a job needs more, and Dependabot proposes weekly updates for every dependency, including the sandbox image's base image.

## If you find personal data in the repository

Treat it as a vulnerability and report it the same way. Do not quote the data in a public issue.
