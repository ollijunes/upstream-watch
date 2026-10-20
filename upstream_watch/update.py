"""Explicit fast-forward updates, gated by the last successful check."""

import fnmatch
import sys
from datetime import UTC, datetime

from . import gitutil as git
from . import state
from .config import Config, expand
from .discover import Repo, parse_remote


def automatic(repo: Repo, config: Config) -> bool:
    return any(
        value.casefold() == repo.full_name.casefold()
        or expand(value).resolve() == repo.path.resolve()
        for value in config.auto_update
    )


def refusal(
    repo: Repo,
    snapshot: dict | None,
    *,
    allow_untracked: bool = False,
    ignore_flags: bool = False,
    now: datetime | None = None,
) -> tuple[str, int]:
    if not snapshot:
        return "no successful check", 0
    git.validate(repo.path, repo.remote, "remote")
    git.validate(repo.path, snapshot["default_branch"], "branch")
    git.validate_sha(snapshot["tip"])
    common = git.text(repo.path, "rev-parse", "--git-common-dir")
    if str((repo.path / common).resolve()) != repo.common_dir:
        return "repository store changed since check", 0
    remote_name = parse_remote(git.text(repo.path, "remote", "get-url", repo.remote))
    if (
        not remote_name
        or remote_name.casefold() != snapshot.get("remote_full_name", repo.full_name).casefold()
    ):
        return "remote URL changed since check", 0
    branch = snapshot["default_branch"]
    local = git.local_status(repo.path, repo.remote, branch)
    if not local["branch"]:
        return "detached HEAD", 0
    if not local["tracks"]:
        return "branch does not track the watched default branch", 0
    status = git.status(repo.path)
    entries = [entry for entry in status.split("\0") if entry]
    if any(not (allow_untracked and entry.startswith("?? ")) for entry in entries):
        return "working tree or index is not clean", 0
    for name in (
        "MERGE_HEAD",
        "rebase-merge",
        "rebase-apply",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "BISECT_LOG",
        "BISECT_START",
        "sequencer",
    ):
        path = repo.path / git.text(repo.path, "rev-parse", "--git-path", name)
        if path.exists():
            return "merge, rebase, cherry-pick or bisect in progress", 0
    if local["ahead"]:
        return "local branch is ahead", 0
    try:
        age = (
            (now or datetime.now(UTC)) - datetime.fromisoformat(snapshot["checked_at"])
        ).total_seconds()
    except (ValueError, TypeError):
        return "invalid check timestamp", 0
    if not 0 <= age < 86400:
        return "last check is stale", 0
    if snapshot["tip"] != git.ref(repo.path, f"refs/remotes/{repo.remote}/{branch}"):
        return "remote tip changed since check; run check again", 0
    advertised = git.text(repo.path, "ls-remote", "--", repo.remote, f"refs/heads/{branch}")
    rows = advertised.splitlines()
    if len(rows) != 1 or git.validate_sha(rows[0].split("\t")[0]) != snapshot["tip"]:
        return "remote tip changed since check; run check again", 0
    if git.run(
        repo.path, "merge-base", "--is-ancestor", "HEAD", snapshot["tip"], check=False
    ).returncode:
        return "checked tip is not a fast-forward", 0
    if not ignore_flags and any(
        f["severity"] == "high" for f in snapshot.get("pending_flags", snapshot.get("flags", []))
    ):
        return "high flag in last check", 0
    return "", local["behind"]


def _update_repo(
    repo: Repo,
    snapshot: dict | None,
    config: Config,
    *,
    yes: bool = False,
    dry_run: bool = False,
    allow_untracked: bool = False,
    ignore_flags: bool = False,
    auto_only: bool = False,
    show_flags: bool = True,
    stdin=None,
    approve=None,
) -> str:
    stdin = stdin or sys.stdin
    if repo.full_name.split("/")[0].casefold() in {name.casefold() for name in config.own}:
        return "skipped: own"
    if any(fnmatch.fnmatch(str(repo.path), str(expand(p))) for p in config.exclude):
        return "skipped: excluded"
    auto = automatic(repo, config)
    if auto_only and not auto:
        return "not auto-approved"
    if show_flags:
        for flag in (snapshot or {}).get("pending_flags", (snapshot or {}).get("flags", [])):
            print(f"{repo.full_name}: {flag['severity']}: {git.clean(flag['reason'])}")
    try:
        reason, count = refusal(
            repo, snapshot, allow_untracked=allow_untracked, ignore_flags=ignore_flags
        )
        if reason:
            return f"skipped: {reason}"
        if auto and any(
            f["severity"] in config.auto_update_blocked_by
            for f in snapshot.get("pending_flags", snapshot.get("flags", []))
        ):
            return "skipped: auto_update blocked by flags"
        incomplete = not snapshot.get("complete", True)
        if incomplete:
            why = "; ".join(snapshot.get("incomplete_reasons", ["unknown"]))
            if auto or auto_only:
                return f"skipped: analysis incomplete: {why}"
            if show_flags:
                print(f"{repo.full_name}: analysis incomplete: {why}")
        if dry_run:
            return (
                f"dry-run: {count} commits; git merge --ff-only --no-autostash -- {snapshot['tip']}"
            )
        if incomplete or (not yes and not auto):
            if not stdin.isatty():
                return "skipped: approval required (non-TTY)"
            question = f"{repo.full_name}: update {count} commits? [y/N] "
            if approve is not None:
                answer = approve(question)
            else:
                print(question, end="", flush=True)
                answer = stdin.readline()
            if answer.strip().lower() not in {"y", "yes"}:
                return "declined"
        reason, _ = refusal(
            repo, snapshot, allow_untracked=allow_untracked, ignore_flags=ignore_flags
        )
        if reason:
            return f"skipped: {reason}"
        git.run(repo.path, "merge", "--ff-only", "--no-autostash", "--", snapshot["tip"])
        snapshot["pending_flags"] = []
        return "updated"
    except git.GitError as exc:
        return f"update failed: {exc}"


def update_repo(repo: Repo, snapshot: dict | None, config: Config, **kwargs) -> str:
    with state.lock():
        return _update_repo(repo, snapshot, config, **kwargs)
