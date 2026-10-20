# upstream-watch

## Goal

A small tool that watches public GitHub repositories the user has cloned but does not own: it fetches without touching the working tree, writes a Markdown report of new releases, tags and commits with a short changelog, flags risks (possible new maintainer, force-push, moved or unsigned tag, changed install scripts, build hooks or CI) and updates a clone only with `git pull --ff-only` after approval or when the config allows it. A release is finished when the tests and ruff pass in CI on Linux and macOS, no test touches the network or a real repository, `scripts/check_public_tree.py` and gitleaks report nothing, and the README explains how it differs from myrepos and gita.

## Environments

Local only. The tool runs on the user's own machine, by hand or under launchd, and talks to the user's Git remotes and the public GitHub API. CI (`.github/workflows/ci.yml`) runs the tests on Linux and macOS with Python 3.11 to 3.14 against fixture repositories and a local fake API.

## Manual checks

Before a release, run `check` twice against temporary fixture clones with an upstream force-push, a moved tag and a changed install script in between, and read the Markdown report for clarity and correct flags. Check that `update` refuses a dirty clone and a high flag, and fast-forwards a clean one after approval. Install and remove the launchd agent once.
