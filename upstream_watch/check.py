"""Fetch-only checking with per-repository transactional snapshots."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import changelog
from . import gitutil as git
from . import state as storage
from .discover import Repo
from .flags import ORDER, Flag, file_flags, identities, maintainer_flags, tag_details
from .github import GitHub, NotPublic


@dataclass
class Result:
    repo: Repo
    flags: list[Flag] = field(default_factory=list)
    commits: dict = field(
        default_factory=lambda: {"count": 0, "groups": {}, "merges": 0, "merge_subjects": []}
    )
    tags: list[str] = field(default_factory=list)
    releases: list[dict] = field(default_factory=list)
    local: dict = field(default_factory=dict)
    status: str = "checked"
    error: str = ""
    update: str = "not requested"
    notes: list[str] = field(default_factory=list)
    pending: list[Flag] | None = None

    @property
    def highest(self) -> str:
        return max(
            (flag.severity for flag in (self.flags if self.pending is None else self.pending)),
            key=lambda s: ORDER[s],
            default="none",
        )


TAG_LIMIT = 2000


def remote_tags(repo: Repo) -> tuple[dict[str, str], bool]:
    lines = git.text(repo.path, "ls-remote", "--tags", "--", repo.remote).splitlines()
    tags = {}
    truncated = False
    for line in lines:
        sha, name = line.split("\t")
        git.validate_sha(sha)
        if not name.startswith("refs/tags/"):
            raise git.GitError("invalid-tag-name")
        name = name.removeprefix("refs/tags/").removesuffix("^{}")
        git.validate(repo.path, name, "tag")
        if line.endswith("^{}"):
            continue
        if len(tags) < TAG_LIMIT:
            tags[name] = sha
        else:
            truncated = True
    return tags, truncated


def default_branch(repo: Repo) -> str:
    branch = repo.metadata.get("default_branch")
    if not branch:
        result = git.run(repo.path, "ls-remote", "--symref", "--", repo.remote, "HEAD", check=False)
        for line in result.stdout.splitlines():
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                branch = line.split("\t")[0].removeprefix("ref: refs/heads/")
            elif "\t" in line:
                git.validate_sha(line.split("\t")[0])
        if not branch:
            branch = git.text(
                repo.path, "symbolic-ref", f"refs/remotes/{repo.remote}/HEAD"
            ).removeprefix(f"refs/remotes/{repo.remote}/")
    return git.validate(repo.path, branch, "branch")


def pending_flags(old: dict | None, flags: list[Flag], date: str) -> list[Flag]:
    pending = {}
    for value in (old or {}).get("pending_flags", (old or {}).get("flags", [])):
        flag = Flag(**value)
        flag.first_seen = flag.first_seen or (old or {}).get("checked_at", date)[:10]
        pending[(flag.severity, flag.reason)] = flag
    for flag in flags:
        flag.first_seen = date
        pending.setdefault((flag.severity, flag.reason), flag)
    return list(pending.values())


def _check_repo(
    repo: Repo, state: dict, api: GitHub | None = None, *, now: datetime | None = None
) -> Result:
    result = Result(repo)
    old = state["repos"].get(repo.key)
    now = now or datetime.now(UTC)
    incomplete = []
    fetched = False
    try:
        git.validate(repo.path, repo.remote, "remote")
        branch = default_branch(repo)
        tags, truncated = remote_tags(repo)
        if truncated:
            incomplete.append("tag list truncated")
        try:
            fetch = git.run(
                repo.path,
                "fetch",
                "--no-tags",
                "--no-prune",
                "--no-prune-tags",
                "--no-recurse-submodules",
                "--no-write-fetch-head",
                "--no-auto-maintenance",
                "--refmap=",
                repo.remote,
                f"+refs/heads/*:refs/remotes/{repo.remote}/*",
                f"+refs/tags/*:refs/upstream-watch/{repo.remote}/tags/*",
                check=False,
            )
        except git.GitError as exc:
            result.status = "fetch failed"
            result.error = git.redact(str(exc), api.token if api else None)
            return result
        moved = [
            name
            for name, sha in tags.items()
            if old and name in old["tags"] and old["tags"][name] != sha
        ]
        result.flags.extend(Flag("high", f"tag moved: {name}", scope="repo") for name in moved)
        if fetch.returncode:
            result.status = "fetch failed"
            result.error = git.clean(
                git.redact(git.first_error(fetch.stderr), api.token if api else None)
            )
            return result
        fetched = True
        tip = git.ref(repo.path, f"refs/remotes/{repo.remote}/{branch}")
        full_name = repo.metadata.get("full_name", repo.full_name)
        if full_name.casefold() != repo.full_name.casefold() or (
            old and full_name.casefold() != old["full_name"].casefold()
        ):
            result.flags.append(Flag("high", "repository transferred or renamed", scope="repo"))
        if repo.metadata.get("archived"):
            result.flags.append(Flag("info", "archived"))
        result.local = git.local_status(repo.path, repo.remote, branch)
        signed = set(old["signed_tags"]) if old else set()
        verified = set(old.get("verified_tags", [])) if old else set()
        taggers = set()
        new_tags = sorted(set(tags) - set(old["tags"])) if old else []
        for name, sha in tags.items():
            if git.ref(repo.path, f"refs/upstream-watch/{repo.remote}/tags/{name}") != sha:
                incomplete.append("tag changed during check")
                continue
            signature, tagger, annotated = tag_details(
                repo.path, f"refs/upstream-watch/{repo.remote}/tags/{name}"
            )
            if signature:
                signed.add(name)
            if tagger:
                taggers.add(tagger)
            if old and name in new_tags:
                if not signature and old["signed_tags"]:
                    result.flags.append(
                        Flag("review", f"unsigned tag after signed tags: {name}", scope="repo")
                    )
                if tagger and tagger not in old["identities"]:
                    result.flags.append(
                        Flag("review", f"new maintainer signal: new release tagger for {name}")
                    )
            if api and api.token and annotated and (not old or name in new_tags):
                verification = api.verification(repo.full_name, name)
                if verification and verification.get("verified"):
                    verified.add(name)
                elif old and old.get("verified_tags") and verification is not None:
                    result.flags.append(
                        Flag(
                            "review",
                            f"tag not API verified: {name} "
                            f"({verification.get('reason', 'unknown')})",
                            scope="repo",
                        )
                    )
        if api and not api.token:
            result.notes.append("API tag verification skipped: no token available")
        releases = []
        if api:
            try:
                releases = api.releases(repo.full_name)
            except NotPublic:
                result.notes.append("GitHub releases unavailable")
        latest = releases[0].get("tag_name") if releases else old.get("release") if old else None
        if old:
            result.tags = new_tags
            for release in releases:
                if release.get("tag_name") == old.get("release"):
                    break
                result.releases.append(
                    {
                        "name": release.get("name") or release.get("tag_name", ""),
                        "tag": release.get("tag_name", ""),
                        "date": release.get("published_at", ""),
                        "prerelease": bool(release.get("prerelease")),
                        "notes": changelog.release_body(release.get("body") or ""),
                    }
                )
            if branch != old["default_branch"]:
                result.flags.append(Flag("review", "default branch changed"))
            if old["tip"]:
                git.validate_sha(old["tip"])
            available = (
                git.run(
                    repo.path, "cat-file", "-e", old["tip"] + "^{commit}", check=False
                ).returncode
                == 0
            )
            if not available:
                incomplete.append("previous tip no longer available; ancestry unknown")
            elif old["tip"] != tip:
                ancestry = git.run(
                    repo.path, "merge-base", "--is-ancestor", old["tip"], tip, check=False
                )
                if ancestry.returncode == 1:
                    result.flags.append(Flag("high", "force-push: previous tip is not an ancestor"))
                elif ancestry.returncode:
                    raise git.GitError("Cannot determine ancestry")
                revision = f"{old['tip']}..{tip}"
                result.commits = changelog.summarize(repo.path, revision)
                changed, sensitive = file_flags(repo.path, old["tip"], tip)
                result.flags.extend(changed)
                result.flags.extend(
                    maintainer_flags(repo.path, revision, set(old["identities"]), sensitive)
                )
            if repo.metadata.get("fork") and repo.metadata.get("parent"):
                result.flags.append(
                    Flag("info", f"fork with parent {repo.metadata['parent'].get('full_name', '')}")
                )
            if git.text(repo.path, "rev-parse", "--is-shallow-repository") == "true":
                result.flags.append(Flag("info", "shallow clone; limited analysis"))
            if not result.local["branch"]:
                result.flags.append(Flag("info", "detached HEAD"))
        else:
            result.status = "baseline recorded"
        deleted = dict((old or {}).get("deleted_tags", {}))
        if not truncated:
            deleted.update(
                {name: sha for name, sha in (old or {}).get("tags", {}).items() if name not in tags}
            )
        for name, sha in tags.items():
            if name in deleted and name not in (old or {}).get("tags", {}) and deleted[name] != sha:
                result.flags.append(
                    Flag("high", f"tag recreated at a different commit: {name}", scope="repo")
                )
        incomplete.extend(
            f.reason.removeprefix("analysis incomplete: ")
            for f in result.flags
            if f.reason.startswith("analysis incomplete: ")
        )
        result.flags.extend(
            Flag("review", f"analysis incomplete: {why}", scope="repo") for why in incomplete
        )
        current_flags = result.flags
        pending = pending_flags(old, current_flags, now.date().isoformat())
        if result.local["branch"] and git.ref(repo.path, "HEAD") == tip:
            pending = [flag for flag in pending if flag.scope == "repo"]
        incomplete.extend(
            flag.reason.removeprefix("analysis incomplete:").strip()
            for flag in pending
            if flag.reason.startswith("analysis incomplete:")
        )
        # Keep this check's findings visible even when the local tip is already current.
        result.flags = list(
            {(flag.severity, flag.reason): flag for flag in [*current_flags, *pending]}.values()
        )
        result.pending = pending
        known = identities(repo.path, tip) | taggers | set(old["identities"] if old else [])
        snapshot = {
            "tip": tip,
            "default_branch": branch,
            "tags": tags,
            "signed_tags": sorted(signed),
            "verified_tags": sorted(verified),
            "identities": sorted(known),
            "release": latest,
            "full_name": full_name,
            "checked_at": now.isoformat(),
            "complete": not incomplete,
            "incomplete_reasons": list(dict.fromkeys(incomplete)),
            "deleted_tags": deleted,
            "pending_flags": [flag.as_dict() for flag in pending],
            "flags": [flag.as_dict() for flag in current_flags],
            "path": str(repo.path),
            "remote": repo.remote,
            "remote_full_name": repo.full_name,
        }
        state["repos"][repo.key] = git.scrub(snapshot, api.token if api else None)
    except (git.GitError, NotPublic) as exc:
        result.status = (
            "check failed"
            if fetched or str(exc).startswith(("invalid-", "not-a-repo-root"))
            else "fetch failed"
        )
        result.error = git.clean(git.redact(str(exc), api.token if api else None))
    finally:
        if result.status == "fetch failed":
            incomplete = list(dict.fromkeys([*incomplete, "tool namespace fetch failed"]))
            result.flags = pending_flags(
                old,
                [
                    *result.flags,
                    Flag(
                        "review", "analysis incomplete: tool namespace fetch failed", scope="repo"
                    ),
                ],
                now.date().isoformat(),
            )
            incomplete.extend(
                f.reason.removeprefix("analysis incomplete:").strip()
                for f in result.flags
                if f.reason.startswith("analysis incomplete:")
            )
            failed_snapshot = (
                dict(old)
                if old
                else {
                    "tip": "",
                    "default_branch": "",
                    "tags": {},
                    "signed_tags": [],
                    "verified_tags": [],
                    "identities": [],
                    "deleted_tags": {},
                    "release": None,
                    "full_name": repo.full_name,
                    "checked_at": now.isoformat(),
                    "path": str(repo.path),
                    "remote": repo.remote,
                    "remote_full_name": repo.full_name,
                    "flags": [],
                }
            )
            failed_snapshot.update(
                complete=False,
                incomplete_reasons=list(dict.fromkeys(incomplete)),
                pending_flags=[f.as_dict() for f in result.flags],
            )
            state["repos"][repo.key] = git.scrub(failed_snapshot, api.token if api else None)
        if result.error and old:
            result.pending = pending_flags(old, result.flags, now.date().isoformat())
            result.flags = result.pending
        if api and api.token:
            for flag in result.flags:
                flag.reason = api.sanitize(flag.reason)
            for name in ("commits", "releases", "tags", "notes"):
                value = git.scrub(getattr(result, name), api.token)
                setattr(result, name, git.scrub(value, changelog.escape(api.token)))
    return result


def exit_code(results: list[Result]) -> int:
    if results and all(r.status == "fetch failed" for r in results):
        return 2
    highest = max((ORDER.get(r.highest, 0) for r in results), default=0)
    if highest >= 2:
        return 20 if highest == 3 else 10
    return 1 if any(r.error or r.update.startswith("update failed:") for r in results) else 0


def check_repo(repo: Repo, state: dict, api: GitHub | None = None, *, now=None) -> Result:
    with storage.lock():
        return _check_repo(repo, state, api, now=now)
