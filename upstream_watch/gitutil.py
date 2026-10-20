"""Git subprocess boundaries and redacted diagnostics."""

import os
import re
import subprocess
import unicodedata
from pathlib import Path

from .config import WatchError


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


def run(path: Path, *args: str, check: bool = True, timeout: int = 120):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(  # noqa: S603,S607
            ["git", "-C", str(path), *args],  # noqa: S607
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError("git unavailable or timed out") from exc
    if check and result.returncode:
        raise GitError(clean(redact(first_error(result.stderr))))
    return result


def text(path: Path, *args: str) -> str:
    return run(path, *args).stdout.strip()


def ref(path: Path, name: str) -> str:
    return text(path, "rev-parse", "--verify", name)


def local_status(path: Path, remote: str, branch: str) -> dict:
    current = run(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
    upstream = run(path, "rev-parse", "--symbolic-full-name", "@{upstream}", check=False)
    watched = f"refs/remotes/{remote}/{branch}"
    counts = run(path, "rev-list", "--left-right", "--count", f"HEAD...{watched}", check=False)
    ahead, behind = map(int, counts.stdout.split()) if counts.returncode == 0 else (0, 0)
    return {
        "branch": current,
        "tracks": upstream.returncode == 0 and upstream.stdout.strip() == watched,
        "ahead": ahead,
        "behind": behind,
        "dirty": bool(text(path, "status", "--porcelain")),
    }
