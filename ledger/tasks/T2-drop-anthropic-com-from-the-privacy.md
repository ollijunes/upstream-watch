# T2 Drop anthropic.com from the privacy allowlist

- Status: doing
- Lane: chore/privacy-allowlist-20261020
- Evidence: —

## Plan

Goal: the privacy checker allows only example domains, users.noreply.github.com and noreply@github.com; the anthropic.com entry existed for attribution trailers that are no longer used.

Checks: pytest, ruff, `python3 scripts/check_public_tree.py` on the full history.
