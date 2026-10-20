# Security

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/ollijunes/upstream-watch/security/advisories/new).
Do not put a token, a report from your own machine or the list of
repositories you watch in a public issue. Describe the problem with the
fixture repositories from the tests and include the affected version.

## What the tool does

- `check` runs `git fetch --prune --tags --no-recurse-submodules` against
  the watched remote and reads refs and objects. It never runs `pull`,
  `merge`, `rebase`, `checkout` or `reset`, and never writes to the index
  or the working tree of a clone.
- `update` runs exactly one command in a clone, `git pull --ff-only <remote>
  <branch>`, and only after every refusal condition in the README has been
  checked, right before the pull.
- Fetches run with `GIT_TERMINAL_PROMPT=0` and, unless you set
  `GIT_SSH_COMMAND` yourself, with `ssh -o BatchMode=yes`, so a scheduled
  run cannot hang on a password prompt. A fetch that fails is reported and
  the other repositories continue.
- Discovery does not follow symlinks and does not descend into
  `node_modules`, virtual environments, caches or hidden folders.
- The GitHub token, when there is one, comes from `GITHUB_TOKEN` or `gh auth
  token`. It is sent only to `api.github.com` in an `Authorization` header,
  and it is removed from every error message, flag, report and state entry
  before they are written.
- Commit subjects, release notes and file names from upstream are untrusted
  text. Control characters are stripped and Markdown is escaped before they
  go into the report, so a crafted commit message cannot inject headings or
  links into it.
- State and reports are written with mode 600 under
  `~/.local/state/upstream-watch/`. The state keeps SHAs, tag names, ETags
  and SHA-256 hashes of author emails, not the emails.
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
- It does not isolate the fetch. Git runs with your user's permissions and
  your Git configuration, including hooks and credential helpers you have
  set up.
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
