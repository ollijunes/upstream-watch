# Flags

A flag says "look here", not "this is malicious". Most flags have an
innocent explanation most of the time. The point is that you see them before
you pull, not after.

Severities:

- `high`: history or a tag was rewritten, or the repository changed hands.
  `update` refuses these until you pass `--ignore-flags`, and `auto_update`
  skips them by default.
- `review`: something changed that deserves a look at the diff.
- `info`: a fact about the clone or the repository that limits the analysis.

## high

### Force-push

How: the tip recorded at the last check is no longer an ancestor of the new
tip (`git merge-base --is-ancestor`).

Why it matters: commits you had were replaced. A rewritten default branch
is rare in healthy projects and common in takeovers and mistakes.

What to do: compare the old and new history before you pull. `git log
<old>..<new>` and `git log <new>..<old>` in the clone show both sides. A
fast-forward pull is impossible anyway; decide whether to reset to the new
branch or keep your copy.

### Tag moved

How: a tag name seen at an earlier check now points at a different object
in `git ls-remote --tags`.

Why it matters: release tags are supposed to be immutable. A moved tag
means a build from "v2.1.0" today is not the build from "v2.1.0" last
month. Git itself refuses to update the local tag.

What to do: check the project's release notes and issues. If the project
did not announce it, treat the tag as unknown.

### Repository transferred or renamed

How: the `full_name` GitHub returns differs from the owner and name in your
remote URL or from the last check.

Why it matters: GitHub redirects old names, so your remote keeps working
while the project is owned by someone else.

What to do: find out who owns it now and why. Update the remote URL when
you are satisfied.

## review

### Default branch changed

How: the default branch from the API or `git ls-remote --symref` differs
from the last check.

What to do: usually a rename from `master` to `main`, or a new release
line. Check that your local branch tracks the branch you want.

### Unsigned tag after signed tags

How: earlier recorded tags carried a PGP or SSH signature block and a new
tag does not, or is a lightweight tag. With a token, the tool also asks
GitHub whether a new annotated tag is verified, and flags it when earlier
tags were verified and this one is not.

Why it matters: a project that signs releases and suddenly stops may have a
new release process, a new person, or a compromised account.

What to do: read the release announcement. Verify the signature yourself
with the project's published key if you rely on it. The tool does not check
keys.

### New maintainer signal

How: author and committer emails are hashed and compared with the hashes
seen in earlier history. The flag fires when new identities wrote more than
30 % of the new commits, touched an install, build or CI file, or tagged a
release.

Why it matters: a new person with write access and changes to how the
project is built is the pattern behind several supply-chain incidents.

What to do: look at who the new identities are. A large pull request from
a new contributor, merged by a known maintainer, looks the same to the
tool. Read the changes to install and build files.

### Ownership files changed

How: `CODEOWNERS`, `.github/CODEOWNERS`, `docs/CODEOWNERS`, `MAINTAINERS*`,
`OWNERS`, `AUTHORS*`, `SECURITY.md` or `.github/FUNDING.yml` changed.

What to do: read the diff. These files say who is in charge and where money
goes.

### Install, build or CI files changed

How: a file in the install and build list changed between the old and new
tip: `install*`, `setup.sh`, `bootstrap*`, `get*.sh`, shell scripts at the
repository root, `Makefile`, `*.mk`, `CMakeLists.txt`, `configure*`,
`setup.py`, `setup.cfg`, `pyproject.toml`, `build.rs`, `Cargo.toml`,
`.npmrc`, `binding.gyp`, `Gemfile`, `*.gemspec`, `.github/workflows/`,
`.github/actions/`, `action.yml`, `Dockerfile*`, `.pre-commit-hooks.yaml`,
`.gitattributes`, `.gitmodules`, `.husky/`, `.envrc`, `flake.nix`, `*.ps1`,
`Brewfile`, `Formula/*.rb`. For `package.json` only the `preinstall`,
`install`, `postinstall`, `prepare` and `prepublish` scripts count.

Why it matters: these files run code on your machine when you install or
build, often with your user's permissions.

What to do: read the diff of the named files before you build or install.

### New executable or binary files, download piped to a shell

How: `git diff --summary` shows a new file with mode 100755 or a mode
change to it; `git diff --numstat` shows a new binary file; or an added
line matches `curl ... | sh`, `wget ... | bash` or `iwr ... | iex`.

What to do: open the file. A new binary in a source repository and a
one-line installer are both worth a question in the project's issues.

## info

- Archived: the project is read-only on GitHub. Expect no fixes.
- Fork with parent: your clone points at a fork. The real upstream is the
  parent named in the flag.
- Shallow clone: ancestry and identity checks see only part of the history.
  `git fetch --unshallow` in the clone gives the tool the whole picture.
- Detached HEAD: the clone is not on a branch. `update` will refuse it; the
  report still shows what is new.
