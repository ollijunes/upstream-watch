"""Configuration and private, atomic file writes."""

import json
import os
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path


class WatchError(Exception):
    pass


def expand(value: str) -> Path:
    if "\0" in value or (value.startswith("~") and value != "~" and not value.startswith("~/")):
        raise WatchError("Invalid path")
    return Path(value).expanduser().absolute()


def config_path() -> Path:
    return expand(os.environ.get("XDG_CONFIG_HOME", "~/.config")) / "upstream-watch/config.toml"


def state_path() -> Path:
    return expand(os.environ.get("XDG_STATE_HOME", "~/.local/state")) / "upstream-watch/state.json"


def no_symlinks(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise WatchError("Refusing a symlink in a filesystem path")


def private_dir(path: Path) -> None:
    no_symlinks(path)
    if not path.exists():
        private_dir(path.parent)
        path.mkdir(mode=0o700)


def write_private(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    private_dir(path.parent)
    no_symlinks(path)
    fd, name = tempfile.mkstemp(prefix=".upstream-watch-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class Config:
    own: list[str] = field(default_factory=list)
    roots: list[str] = field(default_factory=lambda: ["~/src"])
    max_depth: int = 4
    repos: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    auto_update: list[str] = field(default_factory=list)
    auto_update_blocked_by: list[str] = field(default_factory=lambda: ["high"])
    github_api: bool = True


def load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WatchError("Cannot read config: expected a readable TOML file") from exc
    defaults = asdict(Config())
    if data.keys() - defaults.keys():
        raise WatchError("Unknown config key")
    for key, default in defaults.items():
        value = data.get(key, default)
        if type(value) is not type(default):
            raise WatchError(f"Invalid config type: {key}")
        if isinstance(default, list) and any(not isinstance(v, str) or not v for v in value):
            raise WatchError(f"Config {key} must contain nonempty strings")
    if not 0 <= data.get("max_depth", 4) <= 100:
        raise WatchError("max_depth must be between 0 and 100")
    if set(data.get("auto_update_blocked_by", ["high"])) - {"high", "review", "info"}:
        raise WatchError("Unknown flag severity")
    return Config(**data)


def dump(config: Config) -> bytes:
    return (
        "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in asdict(config).items())
        + "\n"
    ).encode()
