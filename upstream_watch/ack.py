"""Acknowledge saved findings without invoking Git."""

import fnmatch
import sys

from . import state
from .config import WatchError, expand
from .discover import candidates
from .gitutil import clean
from .report import display_path


def acknowledge(config, saved: dict, paths, *, yes: bool = False) -> bool:
    paths = [path.expanduser() for path in paths]
    if any(path.resolve() != path.absolute() for path in paths):
        raise WatchError("symlink")
    wanted = {path.resolve() for path in paths}
    watched = {
        path.resolve()
        for path in candidates(config)
        if path.resolve() == path.absolute()
        and not any(fnmatch.fnmatch(str(path), str(expand(p))) for p in config.exclude)
    }
    own = {name.casefold() for name in config.own}
    selected = []
    for snapshot in saved["repos"].values():
        path = expand(snapshot["path"]).resolve()
        name = snapshot.get("remote_full_name", snapshot["full_name"])
        if path in watched and name.split("/")[0].casefold() not in own:
            if not wanted or path in wanted:
                selected.append(snapshot)
    if wanted - {expand(s["path"]).resolve() for s in selected}:
        raise WatchError("Requested path has no saved state in current configuration")
    changed = False
    for snapshot in selected:
        name = clean(snapshot["full_name"])
        path = clean(display_path(expand(snapshot["path"])))
        print(f"{name} ({path}):")
        pending = snapshot.get("pending_flags", snapshot.get("flags", []))
        if not pending:
            print("  no pending flags")
            continue
        for flag in pending:
            date = flag.get("first_seen") or snapshot["checked_at"][:10]
            print(
                f"- {clean(flag['severity'])}: {clean(flag['reason'])} (first seen {clean(date)})"
            )
        if not yes:
            if not sys.stdin.isatty():
                print(f"{name}: skipped: acknowledgement required (non-TTY)")
                continue
            print(f"{name}: acknowledge pending flags? [y/N] ", end="", flush=True)
            if sys.stdin.readline().strip().lower() not in {"y", "yes"}:
                print(f"{name}: declined")
                continue
        state.clear_pending(snapshot)
        changed = True
        print(f"{name}: acknowledged")
    return changed
