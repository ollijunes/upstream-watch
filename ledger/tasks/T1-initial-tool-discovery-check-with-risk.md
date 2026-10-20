# T1 Initial tool: discovery, check with risk flags, report, ff-only update, launchd

- Status: done
- Lane: feat/initial-tool-20261020
- Evidence: CI 11/11 with tests on Linux and macOS (Intel runner) × Python 3.11–3.14, lint, secrets and ledger: https://github.com/ollijunes/upstream-watch/actions/runs/39483614841

## Plan

Goal: a standard-library CLI that finds cloned public GitHub repositories the user does not own (or reads a list), fetches them without touching the working tree, reports new releases, tags and commits with a condensed changelog, flags force-pushes, moved or unsigned tags, possible new maintainers and changed install, build or CI files, and updates only with `git pull --ff-only` after approval or when the config allows it; Markdown report, launchd schedule, tests, CI and a privacy scan.

Checks: pytest with 90 % coverage, ruff check and format, `scripts/check_public_tree.py`, gitleaks over the full history, a manual run against temporary fixture repositories, no test that touches the network or a real repository.
