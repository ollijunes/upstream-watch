"""Fetch-only checking with per-repository transactional snapshots."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import changelog
from . import gitutil as git
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

    @property
    def highest(self) -> str:
        return max((flag.severity for flag in self.flags), key=lambda s: ORDER[s], default="none")


def remote_tags(repo: Repo) -> dict[str, str]:
    lines = git.text(repo.path, "ls-remote", "--tags", repo.remote).splitlines()
    return {
        name.removeprefix("refs/tags/"): sha
        for sha, name in (line.split("\t") for line in lines)
        if not name.endswith("^{}")
    }


def default_branch(repo: Repo) -> str:
    branch = repo.metadata.get("default_branch")
    if branch:
        if git.run(repo.path, "check-ref-format", f"refs/heads/{branch}", check=False).returncode:
            raise git.GitError("Invalid default branch")
        return branch
    result = git.run(repo.path, "ls-remote", "--symref", repo.remote, "HEAD", check=False)
    for line in result.stdout.splitlines():
        if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
            return line.split("\t")[0].removeprefix("ref: refs/heads/")
    return git.text(repo.path, "symbolic-ref", f"refs/remotes/{repo.remote}/HEAD").removeprefix(
        f"refs/remotes/{repo.remote}/"
    )


def check_repo(
    repo: Repo, state: dict, api: GitHub | None = None, *, now: datetime | None = None
) -> Result:
    result = Result(repo)
    old = state["repos"].get(repo.key)
    try:
        try:
            fetch = git.run(
                repo.path,
                "fetch",
                "--prune",
                "--tags",
                "--no-recurse-submodules",
                "--no-write-fetch-head",
                repo.remote,
                check=False,
            )
        except git.GitError as exc:
            result.status = "fetch failed"
            result.error = git.redact(str(exc), api.token if api else None)
            return result
        try:
            tags = remote_tags(repo)
        except git.GitError:
            if not fetch.returncode:
                raise
            tags = {}
        moved = [
            name
            for name, sha in tags.items()
            if old and name in old["tags"] and old["tags"][name] != sha
        ]
        result.flags.extend(Flag("high", f"tag moved: {name}") for name in moved)
        if fetch.returncode:
            result.status = "fetch failed"
            result.error = git.clean(
                git.redact(git.first_error(fetch.stderr), api.token if api else None)
            )
            return result
        branch = default_branch(repo)
        tip = git.ref(repo.path, f"refs/remotes/{repo.remote}/{branch}")
        full_name = repo.metadata.get("full_name", repo.full_name)
        if full_name.casefold() != repo.full_name.casefold() or (
            old and full_name.casefold() != old["full_name"].casefold()
        ):
            result.flags.append(Flag("high", "repository transferred or renamed"))
        if repo.metadata.get("archived"):
            result.flags.append(Flag("info", "archived"))
        result.local = git.local_status(repo.path, repo.remote, branch)
        signed = set(old["signed_tags"]) if old else set()
        verified = set(old.get("verified_tags", [])) if old else set()
        taggers = set()
        new_tags = sorted(set(tags) - set(old["tags"])) if old else []
        for name, sha in tags.items():
            signature, tagger, annotated = tag_details(repo.path, sha)
            if signature:
                signed.add(name)
            if tagger:
                taggers.add(tagger)
            if old and name in new_tags:
                if not signature and old["signed_tags"]:
                    result.flags.append(Flag("review", f"unsigned tag after signed tags: {name}"))
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
                        "notes": "\n".join((release.get("body") or "").splitlines()[:20]),
                    }
                )
            if branch != old["default_branch"]:
                result.flags.append(Flag("review", "default branch changed"))
            available = (
                git.run(
                    repo.path, "cat-file", "-e", old["tip"] + "^{commit}", check=False
                ).returncode
                == 0
            )
            if not available:
                result.flags.append(
                    Flag("review", "previous tip no longer available; ancestry unknown")
                )
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
            "checked_at": (now or datetime.now(UTC)).isoformat(),
            "flags": [flag.as_dict() for flag in result.flags],
            "path": str(repo.path),
            "remote": repo.remote,
            "remote_full_name": repo.full_name,
        }
        state["repos"][repo.key] = git.scrub(snapshot, api.token if api else None)
    except (git.GitError, NotPublic) as exc:
        result.status = "check failed"
        result.error = git.clean(git.redact(str(exc), api.token if api else None))
    finally:
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
    return 1 if any(r.error for r in results) else 0
