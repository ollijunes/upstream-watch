# Changelog

## 0.1.0

- `list` finds cloned GitHub repositories under configured roots or from a
  file, skips your own repositories, worktree duplicates, submodules and
  non-GitHub remotes, and watches the `upstream` remote of your own forks.
- `check` fetches without touching the working tree and writes a Markdown
  report with new releases, tags, a changelog grouped by commit type and the
  state of the local branch.
- Flags: force-push, moved tag and transferred repository as `high`;
  default branch change, unsigned or unverified tag after signed ones, new
  maintainer signal, ownership file changes, install, build and CI file
  changes, new executables and binaries, and download-to-shell lines as
  `review`; archived, fork, shallow clone and detached HEAD as `info`.
- `update` fast-forwards with `git pull --ff-only` after approval, with
  refusal conditions for dirty, ahead, detached, in-progress and stale
  clones and for `high` flags; `auto_update` with `auto_update_blocked_by`.
- GitHub API with ETags, a rate-limit stop and token redaction; git-only
  mode without the API.
- `schedule` for launchd, with printed systemd units and templates in
  `contrib/`.
- Tests on Linux and macOS with Python 3.11–3.14 against fixture
  repositories and a fake API, and a privacy scan of the full history in CI.
