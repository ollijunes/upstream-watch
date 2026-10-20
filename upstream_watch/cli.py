"""Command-line entry point."""

import argparse
import http.client
import os
import shutil
import subprocess
import sys
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, report, state
from .check import Result, check_repo, exit_code
from .config import Config, WatchError, config_path, dump, expand, load, state_path, write_private
from .discover import discover
from .flags import Flag
from .github import GitHub, runtime_token
from .gitutil import clean, redact, scrub
from .schedule import schedule
from .update import update_repo

EXIT_HELP = (
    "Exit codes: in precedence order, 2 every repository failed to fetch; "
    "20 pending high; 10 pending review; 1 configuration/usage/check/update error; else 0."
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="upstream-watch", description="Watch public GitHub upstreams.", epilog=EXIT_HELP
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--config", type=Path, default=None)
    sub = root.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Write a private configuration")
    init.add_argument("--own", nargs="+", action="extend", default=[])
    init.add_argument("--root", nargs="+", action="extend", default=[])
    init.add_argument("--force", action="store_true")
    listing = sub.add_parser("list", help="Show watched and skipped clones")
    listing.add_argument("--from", dest="source", type=Path)
    check = sub.add_parser(
        "check", help="Fetch and report; never change the working tree", epilog=EXIT_HELP
    )
    check.add_argument("--from", dest="source", type=Path)
    check.add_argument("--report", type=Path)
    check.add_argument("--stdout", action="store_true")
    check.add_argument("--update-allowed", action="store_true")
    update = sub.add_parser("update", help="Approve a checked fast-forward update")
    update.add_argument("paths", type=Path, nargs="*")
    for flag in ("yes", "dry-run", "allow-untracked", "ignore-flags"):
        update.add_argument("--" + flag, action="store_true")
    sched = sub.add_parser("schedule", help="Print a schedule or manage launchd on macOS")
    sched.add_argument("action", choices=("install", "print", "uninstall"))
    sched.add_argument("--at", default="08:00")
    sched.add_argument("--weekday", type=int, choices=range(1, 8))
    sched.add_argument("--update-allowed", action="store_true")
    sched.add_argument("--platform", choices=("launchd", "systemd"), default="launchd")
    sched.add_argument("--dry-run", action="store_true")
    return root


def initialize(args, path: Path) -> int:
    write_private(
        path, dump(Config(own=args.own, roots=args.root or ["~/src"])), exclusive=not args.force
    )
    print(f"Config written: {report.display_path(path)}")
    if not args.own and shutil.which("gh"):
        try:
            result = subprocess.run(  # noqa: S603,S607
                ["gh", "api", "user", "--jq", ".login"],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                print(
                    f"Suggested own account: {clean(result.stdout.strip())}; pass --own to save it"
                )
        except (OSError, subprocess.TimeoutExpired):
            pass
    return 0


def execute(args, token=None) -> int:
    with state.lock() if args.command in {"check", "update"} else nullcontext():
        return _execute(args, token)


def _execute(args, token) -> int:
    cfg_path = args.config or config_path()
    if args.command == "init":
        return initialize(args, cfg_path)
    if args.command == "schedule":
        return schedule(
            args.action,
            Path.home(),
            cfg_path,
            at=args.at,
            weekday=args.weekday,
            update_allowed=args.update_allowed,
            platform=args.platform,
            dry_run=args.dry_run,
        )
    config = load(cfg_path)
    saved_path = state_path()
    saved = state.load(saved_path)
    client = GitHub(saved["api"], token=token)
    if args.command == "update":
        if any(path.resolve() != path.expanduser().absolute() for path in args.paths):
            raise WatchError("symlink")
        wanted = {path.resolve() for path in args.paths}
        found = set()
        results = []
        current = {repo.key: repo for repo in discover(config) if not repo.reason}
        for key, snapshot in saved["repos"].items():
            if key not in current:
                continue
            repo = current[key]
            if wanted and repo.path.resolve() not in wanted:
                continue
            found.add(repo.path.resolve())
            result = Result(repo)
            if key in saved.get("failed_checks", []):
                outcome = "skipped: last check failed"
                result.error = "last check failed"
            else:
                outcome = update_repo(
                    repo,
                    snapshot,
                    config,
                    yes=args.yes,
                    dry_run=args.dry_run,
                    allow_untracked=args.allow_untracked,
                    ignore_flags=args.ignore_flags,
                )
            result.update = outcome
            result.pending = [
                Flag(**f) for f in snapshot.get("pending_flags", snapshot.get("flags", []))
            ]
            results.append(result)
            print(f"{clean(repo.full_name)}: {outcome}")
        state.save(saved_path, scrub(saved, token))
        if wanted - found:
            raise WatchError("Requested path has no successful check in current discovery")
        return exit_code(results)
    api = client if config.github_api else None
    repos = discover(config, api, args.source)
    if args.command == "list":
        for repo in repos:
            status = repo.reason or f"watch {repo.full_name} ({repo.remote})"
            print(f"{clean(report.display_path(repo.path))}: {clean(status)}")
        state.save(saved_path, scrub(saved, token))
        return 0
    state.expire(saved, {r.key for r in repos if not r.reason}, datetime.now(UTC))
    results = [check_repo(repo, saved, api) for repo in repos if not repo.reason]
    failed = set(saved.get("failed_checks", []))
    for result in results:
        if result.error:
            failed.add(result.repo.key)
        else:
            failed.discard(result.repo.key)
    saved["failed_checks"] = sorted(failed)
    state.save(saved_path, scrub(saved, token))
    if args.update_allowed:
        for result in results:
            if not result.error:
                result.update = update_repo(
                    result.repo,
                    saved["repos"].get(result.repo.key),
                    config,
                    auto_only=True,
                    show_flags=False,
                )
                result.pending = [
                    Flag(**flag)
                    for flag in saved["repos"][result.repo.key].get("pending_flags", [])
                ]
    state.save(saved_path, scrub(saved, token))
    content = report.render(results, api.messages if api else [])
    content = redact(redact(content, token), report.escape(token) if token else None)
    if args.stdout:
        print(content, end="")
    else:
        for result in results:
            print(
                f"{clean(result.repo.full_name)}: {result.commits['count']} commits, "
                f"{len(result.tags)} tags, {len(result.releases)} releases; "
                f"{result.highest}; {result.status}"
            )
        destination = args.report or saved_path.parent / "reports" / (
            datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S") + ".md"
        )
        destination = state.write_report(expand(str(destination)), content.encode())
        print(f"Report: {report.display_path(destination)}")
    for result in results:
        if result.update.startswith("update failed:"):
            print(f"{clean(result.repo.full_name)}: {result.update}")
    return exit_code(results)


class RedactingWriter:
    def __init__(self, stream, token):
        self.stream = stream
        self.token = token

    def write(self, value):
        return self.stream.write(redact(value, self.token))

    def flush(self):
        self.stream.flush()


def main(argv=None) -> int:
    token = os.environ.get("GITHUB_TOKEN")
    try:
        token = runtime_token() or token
        with (
            redirect_stdout(RedactingWriter(sys.stdout, token)),
            redirect_stderr(RedactingWriter(sys.stderr, token)),
        ):
            args = parser().parse_args(argv)
            return execute(args, token)
    except state.Busy as exc:
        print(str(exc))
        return 0
    except SystemExit as exc:
        return 0 if exc.code == 0 else 1
    except (WatchError, OSError, ValueError, http.client.HTTPException) as exc:
        print(f"upstream-watch: {clean(redact(str(exc), token))}", file=sys.stderr)
        return 1
