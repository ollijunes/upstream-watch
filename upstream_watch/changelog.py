"""Bounded changelog and plain-text Markdown escaping."""

import re

from . import gitutil as git


def escape(value: str) -> str:
    text = git.clean(value)
    text = re.sub(r"([\\`*_\[\]<>|])", r"\\\1", text)
    text = re.sub(r"^(#|-|\+)", lambda m: "\\" + m[0], text)
    return re.sub(r"^(\d+)\.", r"\1\\.", text)


def cut(value: str, limit: int) -> str:
    return value[:limit] + "…" if len(value) > limit else value


def release_body(value: str) -> str:
    lines = value.splitlines()
    body = "\n".join(cut(line, 300) for line in lines[:20])
    if len(lines) > 20:
        body += "…"
    return cut(body, 4000)


def summarize(path, revision: str) -> dict:
    count = int(git.text(path, "rev-list", "--count", revision))
    subjects = git.text(path, "log", "--no-merges", "--format=%s", "-30", revision).splitlines()
    groups: dict[str, list[str]] = {}
    for subject in subjects:
        match = re.match(
            r"(feat|fix|security|perf|docs|chore|ci|build|test)(?:\([^)]*\))?!?:", subject
        )
        group = match[1] if match else "other"
        if group in {"ci", "build", "test"}:
            group = "chore"
        entries = groups.setdefault(group, [])
        if len(entries) < 10:
            entries.append(escape(cut(subject, 200)))
    merges = int(git.text(path, "rev-list", "--merges", "--count", revision))
    titles = git.text(path, "log", "--merges", "--format=%s", "-10", revision).splitlines()
    return {
        "count": count,
        "groups": groups,
        "merges": merges,
        "merge_subjects": [escape(cut(title, 200)) for title in titles],
    }
