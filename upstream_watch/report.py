"""Markdown report rendering."""

from pathlib import Path

from .changelog import cut, escape
from .check import Result
from .flags import ORDER


def display_path(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def render(results: list[Result], messages: list[str] = ()) -> str:
    lines = [
        "# Upstream watch",
        "",
        "| Repository | Commits | Tags | Releases | Highest flag | Update |",
        "| --- | ---: | ---: | ---: | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| {escape(r.repo.full_name)} | {r.commits['count']} | {len(r.tags)} | "
            f"{len(r.releases)} | {r.highest} | {escape(r.update)} |"
        )
    lines.append("")
    shared_notes = [
        note
        for r in results
        if r.status == "checked"
        and not (r.error or r.flags or r.tags or r.releases or r.commits["count"])
        for note in r.notes
    ]
    for message in dict.fromkeys([*messages, *shared_notes]):
        lines.extend([escape(message), ""])
    flags = [(r, flag) for r in results for flag in r.flags]
    if flags:
        lines.extend(["## Flags", ""])
        for r, flag in sorted(flags, key=lambda pair: -ORDER[pair[1].severity]):
            lines.append(
                f"- **{flag.severity}** {escape(r.repo.full_name)}: {escape(flag.reason)}"
                + (f" (first seen {flag.first_seen})" if flag.first_seen else "")
            )
        lines.append("")
    unchanged = []
    for r in results:
        if r.status == "checked" and not (
            r.error or r.flags or r.tags or r.releases or r.commits["count"]
        ):
            unchanged.append(escape(r.repo.full_name))
            continue
        lines.extend(
            [
                f"## {escape(r.repo.full_name)}",
                "",
                f"Path: {escape(display_path(r.repo.path))}",
                "",
                escape(r.status),
                "",
            ]
        )
        if r.error:
            lines.extend([escape(r.error), ""])
        for note in r.notes:
            lines.extend([escape(note), ""])
        if r.releases:
            lines.extend(["### Releases", ""])
            for release in r.releases:
                lines.extend(
                    [
                        f"- {escape(release['name'])} ({escape(release['tag'])}), "
                        f"{escape(release['date'])}"
                        + (" (prerelease)" if release["prerelease"] else "")
                    ]
                )
                lines.extend(
                    "  " + escape(cut(line, 300)) for line in release["notes"].splitlines()
                )
            lines.append("")
        if r.tags:
            lines.extend(["### Tags", "", *[f"- {escape(tag)}" for tag in r.tags], ""])
        if r.commits["count"]:
            lines.extend(["### Changelog", ""])
            for group, subjects in r.commits["groups"].items():
                lines.extend([f"#### {group}", "", *[f"- {subject}" for subject in subjects], ""])
            lines.extend(
                [
                    f"Merge commits: {r.commits['merges']}",
                    *[f"- {subject}" for subject in r.commits["merge_subjects"]],
                    "",
                ]
            )
        if r.local:
            local = r.local
            tracking = (
                f"Local branch {local['branch']} tracks {local['watched']}"
                if local["tracks"]
                else f"Local: not on the branch that tracks {local['watched']}"
            )
            cleanliness = "working tree has changes" if local["dirty"] else "clean"
            ahead, behind = local["ahead"], local["behind"]
            distance = (
                ", ".join(
                    f"{count} commit{'s' if count != 1 else ''} {direction}"
                    for count, direction in ((ahead, "ahead"), (behind, "behind"))
                    if count
                )
                or "up to date"
            )
            lines.extend([escape(f"{tracking}; {cleanliness}; {distance}."), ""])
    if unchanged:
        lines.extend(["Unchanged: " + ", ".join(unchanged) + ".", ""])
    return "\n".join(lines)
