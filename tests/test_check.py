import copy
import json
import shutil

import pytest

from upstream_watch import gitutil
from upstream_watch.changelog import escape, summarize
from upstream_watch.check import check_repo, default_branch, exit_code
from upstream_watch.flags import identity, risky_path
from upstream_watch.github import GitHub

from .conftest import git


def reasons(result):
    return "\n".join(f.reason for f in result.flags)


def test_baseline_and_no_worktree_changes(clone, saved):
    before = git(clone.path, "rev-parse", "HEAD")
    result = check_repo(clone.repo, saved)
    assert result.status == "baseline recorded"
    assert result.flags == []
    assert result.commits["count"] == 0
    assert saved["repos"][clone.repo.key]["identities"] == [identity("author@example.com")]
    assert "author@example.com" not in json.dumps(saved)
    clone.commit(subject="fix: repair *widget*\x01")
    result = check_repo(clone.repo, saved)
    assert result.commits["count"] == 1
    assert result.commits["groups"]["fix"] == ["fix: repair \\*widget\\*"]
    assert result.local["behind"] == 1
    assert git(clone.path, "rev-parse", "HEAD") == before
    assert git(clone.path, "status", "--porcelain") == ""
    assert not (clone.path / ".git/FETCH_HEAD").exists()
    assert exit_code([result]) == 0


def test_changelog_caps_and_merge_titles(clone):
    old = git(clone.source, "rev-parse", "HEAD")
    for n in range(35):
        git(clone.source, "commit", "--allow-empty", "-m", f"feat: item {n}")
    git(clone.source, "checkout", "-b", "feature", old)
    (clone.source / "other").write_text("other")
    git(clone.source, "add", "other")
    git(clone.source, "commit", "-m", "ci: pipeline")
    git(clone.source, "checkout", "main")
    git(clone.source, "merge", "--no-ff", "feature", "-m", "Merge PR [title]")
    log = summarize(clone.source, f"{old}..HEAD")
    assert log["count"] == 37
    assert len(log["groups"]["feat"]) == 10
    assert log["merges"] == 1
    assert log["merge_subjects"] == ["Merge PR \\[title\\]"]
    assert escape("<script>\x1b\u202etest") == r"\<script\>test"


def test_tags_and_force_push(clone, saved):
    check_repo(clone.repo, saved)
    clone.tag("v1")
    clone.commit()
    result = check_repo(clone.repo, saved)
    assert result.tags == ["v1"]
    git(clone.source, "reset", "--hard", "HEAD~1")
    git(clone.source, "commit", "--allow-empty", "-m", "rewritten")
    git(clone.source, "push", "--force", "origin", "main")
    result = check_repo(clone.repo, saved)
    assert "force-push" in reasons(result)
    assert result.highest == "high"
    assert exit_code([result]) == 20


def test_moved_tag_updates_tool_namespace(clone, saved):
    clone.tag("v1")
    check_repo(clone.repo, saved)
    previous = copy.deepcopy(saved)
    clone.commit()
    git(clone.source, "tag", "-f", "v1")
    git(clone.source, "push", "--force", "origin", "refs/tags/v1")
    result = check_repo(clone.repo, saved)
    assert "tag moved: v1" in reasons(result)
    assert result.status == "checked"
    assert saved["repos"][clone.repo.key]["tags"] != previous["repos"][clone.repo.key]["tags"]


def test_catchup_clears_range_flags_but_keeps_moved_tag(clone, saved):
    clone.tag("v1")
    check_repo(clone.repo, saved)
    clone.commit("install.sh", "echo install\n")
    git(clone.source, "tag", "-f", "v1")
    git(clone.source, "push", "--force", "origin", "refs/tags/v1")
    check_repo(clone.repo, saved)
    snap = saved["repos"][clone.repo.key]
    assert {f["scope"] for f in snap["pending_flags"]} == {"repo", "range"}
    moved = next(f for f in snap["pending_flags"] if f["reason"] == "tag moved: v1")
    snap["pending_flags"].append({"severity": "review", "reason": "legacy range flag"})
    git(clone.path, "merge", "--ff-only", snap["tip"])
    result = check_repo(clone.repo, saved)
    assert saved["repos"][clone.repo.key]["pending_flags"] == [moved]
    assert result.highest == "high"
    assert [f.as_dict() for f in check_repo(clone.repo, saved).pending] == [moved]


def test_catchup_keeps_incomplete_analysis(clone, saved):
    check_repo(clone.repo, saved)
    saved["repos"][clone.repo.key]["tip"] = "a" * 40
    check_repo(clone.repo, saved)
    snap = saved["repos"][clone.repo.key]
    assert snap["pending_flags"][0]["scope"] == "repo"
    assert snap["complete"] is False
    assert check_repo(clone.repo, saved).highest == "review"
    assert saved["repos"][clone.repo.key]["complete"] is False


def test_missing_tip_and_changed_default(clone, saved):
    check_repo(clone.repo, saved)
    saved["repos"][clone.repo.key]["tip"] = "a" * 40
    result = check_repo(clone.repo, saved)
    assert "ancestry unknown" in reasons(result)
    git(clone.source, "branch", "next")
    git(clone.source, "push", "origin", "next")
    clone.repo.metadata = {"default_branch": "next"}
    result = check_repo(clone.repo, saved)
    assert "default branch changed" in reasons(result)


def test_default_branch_fallback(clone, monkeypatch):
    real = gitutil.run

    def unavailable(path, *args, **kwargs):
        if args[0] == "ls-remote":
            return type("Result", (), {"stdout": "", "returncode": 1})()
        return real(path, *args, **kwargs)

    monkeypatch.setattr(gitutil, "run", unavailable)
    assert default_branch(clone.repo) == "main"
    clone.repo.metadata = {"default_branch": "bad branch"}
    with pytest.raises(gitutil.GitError):
        default_branch(clone.repo)


def test_transfer_archived_fork_detached_and_shallow(factory, saved):
    clone = factory(shallow=True)
    clone.repo.metadata = {
        "full_name": "example-org/widget",
        "archived": True,
        "fork": True,
        "parent": {"full_name": "acme/original"},
    }
    first = check_repo(clone.repo, saved)
    assert len(first.flags) == 2
    assert "transferred" in reasons(first)
    assert "archived" in reasons(first)
    assert {f.reason for f in first.pending} == {"repository transferred or renamed"}
    assert first.pending[0].scope == "repo"
    git(clone.path, "checkout", "--detach")
    second = check_repo(clone.repo, saved)
    assert "shallow" in reasons(second)
    assert "fork with parent" in reasons(second)
    assert "detached HEAD" in reasons(second)


def test_install_ownership_executable_binary_and_pipe(clone, saved):
    check_repo(clone.repo, saved)
    clone.commit(
        "install.sh",
        "curl https://example.org/install | bash\n",
        "build: install",
        "new@example.com",
    )
    (clone.source / "install.sh").chmod(0o755)
    (clone.source / "image.bin").write_bytes(b"\x00\x01binary")
    (clone.source / "CODEOWNERS").write_text("* @example-org\n")
    git(clone.source, "add", ".")
    git(clone.source, "commit", "-m", "security: owners")
    git(clone.source, "push", "origin", "main")
    result = check_repo(clone.repo, saved)
    reason = reasons(result)
    for wanted in (
        "install, build or CI",
        "ownership files",
        "executable",
        "binary",
        "pipes a download",
        "new maintainer signal",
    ):
        assert wanted in reason
    assert all(flag.scope == "range" for flag in result.flags)
    assert result.highest == "review"
    assert exit_code([result]) == 10


@pytest.mark.parametrize(
    "name",
    [
        "install.py",
        "setup.sh",
        "bootstrap",
        "get-tool.sh",
        "run.sh",
        "Makefile",
        "rules.mk",
        "CMakeLists.txt",
        "configure.ac",
        "setup.py",
        "setup.cfg",
        "pyproject.toml",
        "build.rs",
        "Cargo.toml",
        "package.json",
        ".npmrc",
        "binding.gyp",
        "Gemfile",
        "lib.gemspec",
        ".github/workflows/test.yml",
        ".github/actions/check/action.yml",
        "action.yml",
        "action.yaml",
        "Dockerfile.dev",
        ".pre-commit-hooks.yaml",
        ".gitattributes",
        ".gitmodules",
        ".husky/pre-commit",
        ".envrc",
        "flake.nix",
        "install.ps1",
        "Brewfile",
        "Formula/tool.rb",
    ],
)
def test_risky_path_families(name):
    assert risky_path(name)


@pytest.mark.parametrize("name", ["README.md", "docs/tool.sh", "src/index.py"])
def test_ordinary_paths(name):
    assert not risky_path(name)


@pytest.mark.parametrize(
    "before,after,flagged",
    [
        (
            {"version": "1", "scripts": {"test": "a"}},
            {"version": "2", "scripts": {"test": "b"}},
            False,
        ),
        ({"scripts": {}}, {"scripts": {"install": "run"}}, True),
        ({"scripts": {"prepare": "run"}}, {}, True),
        ({}, "invalid", True),
        ({}, {"scripts": []}, True),
    ],
)
def test_package_scripts_only(clone, saved, before, after, flagged):
    clone.commit("package.json", json.dumps(before))
    check_repo(clone.repo, saved)
    clone.commit("package.json", json.dumps(after))
    result = check_repo(clone.repo, saved)
    assert ("install, build or CI" in reasons(result)) == flagged


def test_maintainer_share_low_and_sensitive(clone, saved):
    check_repo(clone.repo, saved)
    clone.commit("other", "one", email="new@example.com")
    for i in range(3):
        clone.commit("file.txt", str(i))
    assert "maintainer" not in reasons(check_repo(clone.repo, saved))
    clone.commit("Makefile", "all:\n", email="another@example.com")
    for i in range(3):
        clone.commit("file.txt", str(i + 3))
    assert "maintainer" in reasons(check_repo(clone.repo, saved))


def test_signed_then_unsigned_tags(clone, saved, tmp_path):
    binary = shutil.which("ssh-keygen")
    if not binary:
        pytest.skip("ssh-keygen unavailable")
    import subprocess

    key = tmp_path / "signing"
    subprocess.run(
        [binary, "-t", "ed25519", "-N", "", "-C", "signer@example.com", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    git(clone.source, "config", "gpg.format", "ssh")
    git(clone.source, "config", "user.signingkey", str(key))
    git(clone.source, "tag", "-s", "-m", "signed", "v1")
    git(clone.source, "push", "origin", "--tags")
    check_repo(clone.repo, saved)
    assert saved["repos"][clone.repo.key]["signed_tags"] == ["v1"]
    clone.tag("v2")
    result = check_repo(clone.repo, saved)
    assert "unsigned tag" in reasons(result)
    assert result.pending[0].scope == "repo"
    assert "unsigned tag" in reasons(check_repo(clone.repo, saved))


def test_new_annotated_tagger(clone, saved, monkeypatch):
    check_repo(clone.repo, saved)
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "tagger@example.org")
    clone.tag("v2", annotated=True)
    assert "new release tagger" in reasons(check_repo(clone.repo, saved))


def test_releases_and_verification(clone, saved, fake_api):
    routes, requests, base = fake_api
    token = "test-" + "credential"
    api = GitHub(saved["api"], token=token, base=base)
    clone.tag("v1", annotated=True)
    sha = git(clone.source, "rev-parse", "v1")
    routes["/repos/acme/widget/git/ref/tags/v1"] = (
        200,
        {"object": {"type": "tag", "sha": sha}},
        {},
    )
    routes[f"/repos/acme/widget/git/tags/{sha}"] = (200, {"verification": {"verified": True}}, {})
    routes["/repos/acme/widget/releases?per_page=10"] = (200, [{"tag_name": "v1"}], {})
    assert check_repo(clone.repo, saved, api).status == "baseline recorded"
    assert saved["repos"][clone.repo.key]["verified_tags"] == ["v1"]
    clone.tag("v2", annotated=True)
    sha = git(clone.source, "rev-parse", "v2")
    routes["/repos/acme/widget/git/ref/tags/v2"] = (
        200,
        {"object": {"type": "tag", "sha": sha}},
        {},
    )
    routes[f"/repos/acme/widget/git/tags/{sha}"] = (
        200,
        {"verification": {"verified": False, "reason": "unsigned"}},
        {},
    )
    routes["/repos/acme/widget/releases?per_page=10"] = (
        200,
        [
            {
                "name": "release",
                "tag_name": "v2",
                "body": "\n".join(f"line {n}" for n in range(30)),
                "prerelease": True,
            },
            {"tag_name": "v1"},
        ],
        {},
    )
    result = check_repo(clone.repo, saved, api)
    assert len(result.releases) == 1
    assert len(result.releases[0]["notes"].splitlines()) == 20
    assert "not API verified" in reasons(result)
    assert result.pending[0].scope == "repo"
    assert "Authorization" not in requests[0][1]
    requests.clear()
    api = GitHub({}, base=base)
    clone.tag("v3", annotated=True)
    result = check_repo(clone.repo, saved, api)
    assert "no token" in result.notes[0]
    assert len(requests) == 1


def test_fetch_and_halfway_failure_isolation(clone, factory, saved, monkeypatch):
    other = factory("example-org/tool")
    check_repo(clone.repo, saved)
    previous = copy.deepcopy(saved)
    original = gitutil.run

    def fail(path, *args, **kwargs):
        if path == clone.path and args[0] == "fetch":
            return type(
                "Result",
                (),
                {"returncode": 1, "stderr": "fatal: https://secret@example.com/error\nsecond line"},
            )()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(gitutil, "run", fail)
    result = check_repo(clone.repo, saved)
    assert result.status == "fetch failed"
    assert "secret" not in result.error and "second line" not in result.error
    assert saved["repos"][clone.repo.key]["tip"] == previous["repos"][clone.repo.key]["tip"]
    assert saved["repos"][clone.repo.key]["complete"] is False
    assert exit_code([result]) == 2
    assert check_repo(other.repo, saved).status == "baseline recorded"

    def halfway(path, *args, **kwargs):
        if args[0] == "log":
            raise gitutil.GitError("log failed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(gitutil, "run", halfway)
    previous = copy.deepcopy(saved)
    result = check_repo(clone.repo, saved)
    assert result.status == "check failed"
    assert saved == previous
    assert exit_code([result]) == 10


def test_actual_unreachable_remote_is_fetch_failure(clone, saved):
    git(clone.path, "remote", "set-url", "origin", "https://github.com/acme/missing.git")
    result = check_repo(clone.repo, saved)
    assert result.status == "fetch failed"
    assert exit_code([result]) == 2
    assert saved["repos"][clone.repo.key]["complete"] is False


def test_fetch_timeout_is_fetch_failure(clone, saved, monkeypatch):
    def timeout(*args, **kwargs):
        raise gitutil.GitError("git unavailable or timed out")

    monkeypatch.setattr(gitutil, "run", timeout)
    result = check_repo(clone.repo, saved)
    assert result.status == "fetch failed"
    assert saved["repos"][clone.repo.key]["complete"] is False


def test_all_install_and_ownership_families_in_diff(clone, saved):
    from upstream_watch.flags import BUILD, OWNERSHIP

    check_repo(clone.repo, saved)
    paths = [pattern.replace("*", "example") for pattern in BUILD + OWNERSHIP]
    paths += [
        ".github/workflows/test.yml",
        ".github/actions/test/action.yml",
        ".husky/pre-commit",
        "Formula/tool.rb",
    ]
    for name in paths:
        target = clone.source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("example\n")
    git(clone.source, "add", ".")
    git(clone.source, "commit", "-m", "build: change hooks")
    git(clone.source, "push", "origin", "main")
    result = check_repo(clone.repo, saved)
    for name in paths:
        assert any(flag.reason.endswith(": " + name) for flag in result.flags), name
