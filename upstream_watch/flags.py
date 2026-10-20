"""Signals from Git metadata and selected changed content."""

import fnmatch
import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from . import gitutil as git

ORDER = {"high": 3, "review": 2, "info": 1}
OWNERSHIP = (
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "docs/CODEOWNERS",
    "MAINTAINERS*",
    "OWNERS",
    "AUTHORS*",
    "SECURITY.md",
    ".github/FUNDING.yml",
)
BUILD = (
    "install*",
    "setup.sh",
    "bootstrap*",
    "get*.sh",
    "*.sh",
    "Makefile",
    "*.mk",
    "CMakeLists.txt",
    "configure*",
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "build.rs",
    "Cargo.toml",
    ".npmrc",
    "binding.gyp",
    "Gemfile",
    "*.gemspec",
    "action.yml",
    "action.yaml",
    "Dockerfile*",
    ".pre-commit-hooks.yaml",
    ".gitattributes",
    ".gitmodules",
    ".envrc",
    "flake.nix",
    "*.ps1",
    "Brewfile",
)
HOOKS = {"preinstall", "install", "postinstall", "prepare", "prepublish"}
PIPE = re.compile(r"\b(?:curl|wget)\b.*\|\s*(?:(?:/\w+)+/)?(?:sh|bash)\b|\biwr\b.*\|\s*iex\b", re.I)


@dataclass
class Flag:
    severity: str
    reason: str

    def as_dict(self):
        return asdict(self)


def identity(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def identities(path, revision: str) -> set[str]:
    output = git.text(path, "log", "--format=%ae%x00%ce", revision, "--")
    return {identity(email) for line in output.splitlines() for email in line.split("\0") if email}


def risky_path(name: str) -> bool:
    base = PurePosixPath(name).name
    if base == "package.json":
        return True
    if name.startswith((".github/workflows/", ".github/actions/", ".husky/")):
        return True
    if fnmatch.fnmatch(name, "Formula/*.rb"):
        return True
    return any(fnmatch.fnmatch(base, p) and (p != "*.sh" or "/" not in name) for p in BUILD)


def package_changed(path, old: str, new: str, name: str) -> bool:
    values = []
    for revision in (old, new):
        result = git.run(path, "show", f"{revision}:{name}", check=False)
        try:
            data = json.loads(result.stdout) if result.returncode == 0 else {}
            scripts = data.get("scripts", {})
            values.append({k: scripts[k] for k in HOOKS if k in scripts})
            if not isinstance(scripts, dict):
                return True
        except (ValueError, AttributeError, TypeError):
            return True
    return values[0] != values[1]


def file_flags(path, old: str, new: str) -> tuple[list[Flag], set[str]]:
    names = git.run(path, "diff", "--name-only", "-z", old, new, "--").stdout.split("\0")
    names = [name for name in names if name]
    numstat = git.run(path, "diff", "--numstat", "-z", "--no-renames", old, new, "--").stdout
    binary = {row.split("\t", 2)[2] for row in numstat.split("\0") if row.startswith("-\t-\t")}
    flags = []
    sensitive = set()
    for name in names:
        if any(fnmatch.fnmatch(name, pattern) for pattern in OWNERSHIP):
            flags.append(Flag("review", f"ownership files changed: {name}"))
        if risky_path(name) and (
            PurePosixPath(name).name != "package.json" or package_changed(path, old, new, name)
        ):
            flags.append(Flag("review", f"install, build or CI files changed: {name}"))
            sensitive.add(name)
    summary = git.text(path, "diff", "--summary", old, new, "--")
    if re.search(r"(?:create mode 100755|mode change \d+ => 100755)", summary):
        flags.append(Flag("review", "new executable files or executable mode changes"))
    added = set(
        git.run(path, "diff", "--diff-filter=A", "--name-only", "-z", old, new).stdout.split("\0")
    )
    if binary & added:
        flags.append(Flag("review", "new binary files"))
    textual = [name for name in names if name not in binary]
    if textual:
        diff = git.run(
            path, "diff", "--no-ext-diff", "--no-textconv", "--unified=0", old, new, "--", *textual
        ).stdout
        if any(
            PIPE.search(line[1:])
            for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ):
            flags.append(Flag("review", "added line pipes a download to a shell"))
    return flags, sensitive


def maintainer_flags(path, revision: str, known: set[str], sensitive: set[str]) -> list[Flag]:
    rows = git.text(path, "log", "--format=%H%x00%ae%x00%ce", revision, "--").splitlines()
    counts: Counter = Counter()
    risky_new = set()
    for row in rows:
        sha, author, committer = row.split("\0")
        new_ids = {identity(author), identity(committer)} - known
        counts.update(new_ids)
        if new_ids and sensitive:
            files = set(
                git.run(
                    path,
                    "diff-tree",
                    "--root",
                    "-m",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "-z",
                    sha,
                ).stdout.split("\0")
            )
            if files & sensitive:
                risky_new |= new_ids
    signals = {value for value, count in counts.items() if count > len(rows) * 0.3} | risky_new
    return (
        [
            Flag(
                "review",
                f"new maintainer signal: {len(signals)} new identities "
                "with substantial or install/build activity",
            )
        ]
        if signals
        else []
    )


def tag_details(path, sha: str) -> tuple[bool, str | None, bool]:
    kind = git.text(path, "cat-file", "-t", sha)
    if kind != "tag":
        return False, None, False
    body = git.run(path, "cat-file", "tag", sha).stdout
    signed = any(
        marker in body
        for marker in ("-----BEGIN PGP SIGNATURE-----", "-----BEGIN SSH SIGNATURE-----")
    )
    match = re.search(r"^tagger .* <([^<>]+)> \d+", body, re.M)
    return signed, identity(match[1]) if match else None, True
