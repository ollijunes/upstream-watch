"""Git subprocess boundaries and redacted diagnostics."""

import os
import re
import subprocess
import tempfile
import unicodedata
from contextvars import ContextVar
from pathlib import Path

from .config import WatchError, private_dir, state_path


class GitError(WatchError):
    pass


def clean(text: str) -> str:
    return "".join(
        c for c in text if not unicodedata.category(c).startswith("C") and c not in "\u2028\u2029"
    )


def first_error(stderr: str) -> str:
    lines = stderr.strip().splitlines()
    return next(
        (line for line in lines if re.search(r"fatal:|error:|!\s+\[rejected\]", line)),
        lines[0] if lines else "git command failed",
    )


def redact(text: str, token: str | None = None) -> str:
    if token:
        text = text.replace(token, "[redacted]")
    text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[redacted]@", text)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)[^\r\n]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)([?&](?:token|access_token|key)=)[^&\s]+", r"\1[redacted]", text)
    return text


def scrub(value, token: str | None):
    if isinstance(value, str):
        return redact(value, token)
    if isinstance(value, dict):
        return {redact(key, token): scrub(item, token) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item, token) for item in value]
    return value


_verified = ContextVar("verified_roots", default=None)
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
CONFIG = (
    "core.fsmonitor=false",
    "maintenance.auto=false",
    "gc.auto=0",
    "fetch.prune=false",
    "fetch.pruneTags=false",
    "merge.autoStash=false",
    "rebase.autoStash=false",
    "diff.external=",
    "diff.noprefix=false",
    "status.showUntrackedFiles=all",
    "protocol.ext.allow=never",
)


def environment() -> dict:
    allowed = {"GIT_SSH_COMMAND", "GIT_SSH", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
        or key in allowed
        or key.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))
    }
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def run(
    path: Path, *args: str, check: bool = True, timeout: int = 120, max_bytes: int | None = None
):
    directory = state_path().parent
    private_dir(directory)
    with tempfile.TemporaryDirectory(prefix="hooks-", dir=directory) as hooks:
        argv = ["git", "-C", str(path)]
        for value in (f"core.hooksPath={hooks}", *CONFIG):
            argv.extend(("-c", value))

        def execute(arguments, limit=None):
            try:
                with tempfile.TemporaryFile() as output:
                    result = subprocess.run(  # noqa: S603
                        [*argv, *arguments],
                        stdout=output if limit is not None else subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                        env=environment(),
                        check=False,
                    )
                    result.truncated = False
                    if limit is not None:
                        output.seek(0)
                        raw = output.read(limit + 1)
                        result.truncated = len(raw) > limit
                        result.stdout = raw[:limit].decode("utf-8", errors="replace")
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise GitError("git unavailable or timed out") from exc
            return result

        verified = _verified.get()
        key = str(path.absolute())
        if verified is None or key not in verified:
            root = execute(("rev-parse", "--show-toplevel"))
            if root.returncode or Path(root.stdout.strip()).resolve() != path.resolve():
                raise GitError("not-a-repo-root")
            if verified is not None:
                verified.add(key)
        result = execute(args, max_bytes)
    if check and result.returncode:
        raise GitError(clean(redact(first_error(result.stderr))))
    return result


def validate(path: Path, name: str, kind: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or any(unicodedata.category(c).startswith("C") for c in name)
        or any(part.startswith("-") for part in name.split("/"))
    ):
        raise GitError(f"invalid-{kind}-name")
    arguments = {
        "branch": ("--branch", name),
        "tag": (f"refs/tags/{name}",),
        "remote": ("--allow-onelevel", name),
    }[kind]
    checked = run(path, "check-ref-format", *arguments, check=False)
    if checked.returncode or (kind == "branch" and checked.stdout.strip() != name):
        raise GitError(f"invalid-{kind}-name")
    return name


def validate_sha(value: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise GitError("invalid-remote-sha")
    return value


def status(path: Path) -> str:
    return run(
        path, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"
    ).stdout


def text(path: Path, *args: str) -> str:
    return run(path, *args).stdout.strip()


def ref(path: Path, name: str) -> str:
    return validate_sha(text(path, "rev-parse", "--verify", "--end-of-options", name))


def local_status(path: Path, remote: str, branch: str) -> dict:
    validate(path, remote, "remote")
    validate(path, branch, "branch")
    current = run(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
    if current:
        validate(path, current, "branch")
    upstream = run(path, "rev-parse", "--symbolic-full-name", "@{upstream}", check=False)
    watched = f"refs/remotes/{remote}/{branch}"
    counts = run(path, "rev-list", "--left-right", "--count", f"HEAD...{watched}", check=False)
    ahead, behind = map(int, counts.stdout.split()) if counts.returncode == 0 else (0, 0)
    return {
        "branch": current,
        "tracks": upstream.returncode == 0 and upstream.stdout.strip() == watched,
        "ahead": ahead,
        "behind": behind,
        "dirty": bool(status(path)),
        "watched": f"{remote}/{branch}",
    }
