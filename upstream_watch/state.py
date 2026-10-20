"""Persistent check snapshots."""

import fcntl
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .config import WatchError, no_symlinks, private_dir, state_path, write_private


def load(path: Path) -> dict:
    if not path.exists():
        return {"repos": {}, "api": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(
            isinstance(data.get(k), dict) for k in ("repos", "api")
        ):
            raise ValueError
        return data
    except (OSError, ValueError) as exc:
        raise WatchError("Cannot read state: expected a valid state JSON file") from exc


def save(path: Path, data: dict) -> None:
    write_private(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode())


def clear_pending(snapshot: dict) -> None:
    snapshot["pending_flags"] = []
    snapshot["complete"] = True
    snapshot["incomplete_reasons"] = []


class Busy(WatchError):
    pass


_held = ContextVar("state_lock", default=None)


@contextmanager
def lock():
    from .gitutil import _verified

    path = state_path().parent / "lock"
    if _held.get() == (os.getpid(), path):
        yield
        return
    private_dir(path.parent)
    no_symlinks(path)
    with path.open("a") as stream:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy("another upstream-watch run is in progress") from exc
        held = _held.set((os.getpid(), path))
        verified = _verified.set(set())
        try:
            yield
        finally:
            _verified.reset(verified)
            _held.reset(held)
            fcntl.flock(stream, fcntl.LOCK_UN)


def write_report(path: Path, content: bytes) -> Path:
    candidate = path
    number = 1
    while True:
        try:
            write_private(candidate, content, exclusive=True)
            return candidate
        except FileExistsError:
            number += 1
            candidate = path.with_name(f"{path.stem}-{number}{path.suffix}")


def expire(data: dict, seen: set[str], now):
    from datetime import datetime
    from urllib.parse import urlsplit

    removed = set()
    for key, snapshot in list(data["repos"].items()):
        if key not in seen and (now - datetime.fromisoformat(snapshot["checked_at"])).days >= 90:
            removed.add(snapshot.get("remote_full_name", snapshot["full_name"]))
            del data["repos"][key]
    retained = {s.get("remote_full_name", s["full_name"]) for s in data["repos"].values()}
    for url in list(data["api"]):
        route = urlsplit(url).path
        if any(
            route == f"/repos/{name}" or route.startswith(f"/repos/{name}/")
            for name in removed - retained
        ):
            del data["api"][url]
    data["failed_checks"] = [k for k in data.get("failed_checks", []) if k in data["repos"]]
