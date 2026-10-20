"""Persistent check snapshots."""

import json
from pathlib import Path

from .config import WatchError, write_private


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
