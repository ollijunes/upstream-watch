"""Bounded clone discovery and upstream selection."""

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import gitutil as git
from .config import Config, expand
from .github import GitHub, NotPublic

SKIP = {"node_modules", ".venv", "venv", "vendor", ".cache", "Library"}


@dataclass
class Repo:
    path: Path
    remote: str = ""
    full_name: str = ""
    common_dir: str = ""
    metadata: dict = field(default_factory=dict)
    reason: str = ""

    @property
    def key(self) -> str:
        return self.common_dir + "::" + self.remote


def parse_remote(url: str) -> str | None:
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?",
        url,
    )
    return match[1] if match else None


def candidates(config: Config, source: Path | None = None):
    if source is not None:
        for line in source.read_text(encoding="utf-8").splitlines():
            value = line.split("#", 1)[0].strip()
            if value:
                yield expand(value)
        return
    for root in map(expand, config.roots):
        if root.resolve() != root.expanduser().absolute():
            yield root
            continue
        for directory, dirs, files in os.walk(root, followlinks=False):
            path = Path(directory)
            if ".git" in dirs or ".git" in files or {"HEAD", "config"} <= set(files):
                yield path
                dirs[:] = []
                continue
            depth = len(path.relative_to(root).parts)
            dirs[:] = [
                d
                for d in dirs
                if depth < config.max_depth
                and d not in SKIP
                and not d.startswith(".")
                and not (path / d).is_symlink()
            ]
    yield from map(expand, config.repos)


def discover(config: Config, api: GitHub | None = None, source: Path | None = None) -> list[Repo]:
    results = []
    seen = set()
    own = {name.lower() for name in config.own}
    for path in candidates(config, source):
        repo = Repo(path)
        results.append(repo)
        if any(fnmatch.fnmatch(str(path), str(expand(p))) for p in config.exclude):
            repo.reason = "excluded"
            continue
        if path.resolve() != path.expanduser().absolute():
            repo.reason = "symlink"
            continue
        try:
            if git.text(path, "rev-parse", "--show-superproject-working-tree"):
                repo.reason = "worktree-duplicate"
                continue
            common = git.text(path, "rev-parse", "--git-common-dir")
            repo.common_dir = str((path / common).resolve())
            if repo.common_dir in seen:
                repo.reason = "worktree-duplicate"
                continue
            seen.add(repo.common_dir)
            remotes = git.text(path, "remote").splitlines()
            if not remotes:
                repo.reason = "no-remote"
                continue
            for remote in remotes:
                git.validate(path, remote, "remote")
            pairs = [(r, parse_remote(git.text(path, "remote", "get-url", r))) for r in remotes]
            github = [(r, n) for r, n in pairs if n]
            external = [(r, n) for r, n in github if n.split("/")[0].lower() not in own]
            if not external:
                repo.reason = "own" if github else "not-github"
                continue
            origin_own = any(r == "origin" and n.split("/")[0].lower() in own for r, n in github)
            preferred = ("upstream", "origin") if origin_own else ("origin", "upstream")
            external.sort(key=lambda pair: preferred.index(pair[0]) if pair[0] in preferred else 2)
            repo.remote, repo.full_name = external[0]
            if api and config.github_api:
                repo.metadata = api.metadata(repo.full_name)
        except NotPublic:
            repo.reason = "not-public"
        except git.GitError as exc:
            repo.reason = (
                str(exc) if str(exc) in {"not-a-repo-root", "invalid-remote-name"} else "no-remote"
            )
    return results
