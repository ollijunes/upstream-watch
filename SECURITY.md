# Security

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/ollijunes/upstream-watch/security/advisories/new).
Do not put a token, a report from your own machine or the list of
repositories you watch in a public issue. Describe the problem with the
fixture repositories from the tests and include the affected version.

## What the tool does

- `check` runs `git fetch` with explicit refspecs into
  `refs/remotes/<remote>/*` and a tool-owned tag namespace, without
  `--prune` and without tags into `refs/tags/`. Your clone's
  `remote.<name>.fetch`, `fetch.prune` and `fetch.pruneTags` settings are
  not used, so local branches and tags are never written. It never runs
  `pull`, `merge`, `rebase`, `checkout` or `reset`, and never writes to the
  index or the working tree of a clone.
- The only command that changes a clone is `git merge --ff-only <commit>`
  in `update`, where `<commit>` is the exact tip the last check recorded.
  There is no second fetch. Every refusal condition in the README is
  checked again immediately before the merge, under a lock that keeps two
  runs of the tool apart.
- Every git command runs with hooks, `fsmonitor`, automatic maintenance,
  auto-stash and external diff drivers turned off, with `GIT_*` variables
  that select a repository (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`
  and the like) removed from the environment, and only after the tool has
  checked that Git resolves the supplied path to that clone's top level.
  Branch, tag and remote names from a remote are validated and passed as
  fully qualified refs, so a name like `--upload-pack=…` cannot become an
  option.
- Fetches run with `GIT_TERMINAL_PROMPT=0` and, unless you set
  `GIT_SSH_COMMAND` yourself, with `ssh -o BatchMode=yes`, so a scheduled
  run cannot hang on a password prompt. A fetch that fails is reported and
  the other repositories continue.
- Discovery does not follow symlinks and does not descend into
  `node_modules`, virtual environments, caches or hidden folders.
- The GitHub token, when there is one, comes from `GITHUB_TOKEN` or `gh auth
  token --hostname github.com`. It is sent only to `https://api.github.com`
  in an `Authorization` header, redirects to any other origin are refused,
  and it is removed from every error message, flag, report, cached response
  and state entry before they are written.
- Flags stay pending in the state, so a `high` flag cannot be cleared by
  running `check` again. Flags about incoming commits clear when the clone
  has caught up; flags about the repository itself, such as a moved tag, a
  transfer or an incomplete analysis, clear only through `ack` or an update
  made with the tool. `ack` changes the state file, never a clone.
- Commit subjects, release notes and file names from upstream are untrusted
  text. Control characters are stripped and Markdown is escaped before they
  go into the report, so a crafted commit message cannot inject headings or
  links into it.
- State and reports are written with mode 600 under
  `~/.local/state/upstream-watch/`. The state keeps SHAs, tag names, ETags,
  the few API fields the tool uses and SHA-256 hashes of author emails, not
  the emails or raw API responses. Entries for clones not seen for 90 days
  are dropped.
- No code from a watched repository is executed, imported or evaluated.
  `package.json` is parsed as JSON only.

## What it does not do

- It does not verify signatures. It reports whether a tag has a signature
  block and, with a token, what GitHub says. Verify keys yourself.
- Its flags are heuristics. A clean report does not mean an update is safe,
  and a flag does not mean an attack. Read the diff for anything you run
  with your own credentials.
- It trusts `git` and the remote. A compromised remote can serve any
  history; the tool can only tell you that it differs from what you had.
- It does not isolate the fetch. Git runs with your user's permissions,
  your credential helpers and the rest of your Git configuration. Hooks and
  maintenance are turned off for the tool's own commands, but a remote can
  still send any objects it likes and Git parses them with your Git version.
- Without a token, GitHub allows 60 API requests per hour. The tool stops
  calling the API when the limit is reached and says so in the report;
  releases and verification are then missing for that run.

## Repository hygiene

This repository contains no real repository paths, hostnames, tokens or
report output from a real machine. Fixture repositories use the owners
`acme`, `example-org` and `me-example`. CI scans the full Git history with
Gitleaks and with `scripts/check_public_tree.py`, which looks for private
network addresses, home-directory paths, email addresses outside example
domains and private files. Automated scans support a manual review; they do
not prove that nothing private slipped in.
