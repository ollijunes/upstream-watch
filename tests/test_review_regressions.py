import http.client
import io
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from upstream_watch import cli, gitutil, report, state
from upstream_watch.changelog import escape, release_body
from upstream_watch.check import check_repo, exit_code
from upstream_watch.config import config_path, dump, state_path, write_private
from upstream_watch.discover import discover
from upstream_watch.flags import download_to_shell
from upstream_watch.github import GitHub
from upstream_watch.update import update_repo

from .conftest import git
from .test_update import TTY


def snapshot(clone, saved):
    return saved["repos"][clone.repo.key]


def test_fetch_mapping_local_tags_and_hooks(clone, saved, tmp_path):
    before = git(clone.path, "rev-parse", "HEAD")
    git(clone.path, "branch", "victim")
    git(clone.path, "tag", "local-only")
    git(clone.path, "config", "remote.origin.fetch", "+refs/heads/main:refs/heads/victim")
    git(clone.path, "config", "fetch.pruneTags", "true")
    git(clone.path, "config", "remote.origin.prune", "true")
    git(clone.path, "config", "remote.origin.pruneTags", "true")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    for name in ("reference-transaction", "post-merge"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
    git(clone.path, "config", "core.hooksPath", str(hooks))
    check_repo(clone.repo, saved)
    tip = clone.commit()
    clone.tag("v1", annotated=True)
    assert not check_repo(clone.repo, saved).error
    assert git(clone.path, "rev-parse", "refs/heads/victim") == before
    assert git(clone.path, "rev-parse", "refs/tags/local-only") == before
    assert git(clone.path, "show-ref", "--verify", "refs/tags/v1", check=False) == ""
    assert git(clone.path, "rev-parse", "refs/upstream-watch/origin/tags/v1")
    git(clone.path, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    assert update_repo(clone.repo, snapshot(clone, saved), clone.config(), yes=True) == "updated"
    assert git(clone.path, "rev-parse", "HEAD") == tip
    assert not marker.exists()


def test_git_environment_targets_supplied_clone(clone, factory, monkeypatch):
    other = factory("acme/other")
    other.commit()
    for name, value in {
        "GIT_DIR": str(other.path / ".git"),
        "GIT_WORK_TREE": str(other.path),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/unsafe",
        "GIT_ALLOW_PROTOCOL": "ext",
    }.items():
        monkeypatch.setenv(name, value)
    assert gitutil.text(clone.path, "rev-parse", "--show-toplevel") == str(clone.path)
    env = gitutil.environment()
    assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_COUNT", "GIT_ALLOW_PROTOCOL"} & env.keys()
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


@pytest.mark.parametrize("kind", ["branch", "tag"])
def test_option_like_remote_names_never_execute(clone, saved, tmp_path, kind):
    marker = tmp_path / "executed"
    helper = tmp_path / "helper"
    helper.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    helper.chmod(0o755)
    name = f"--upload-pack={helper}" if kind == "branch" else "--upload-pack=x"
    prefix = "heads" if kind == "branch" else "tags"
    git(
        clone.upstream,
        "update-ref",
        f"refs/{prefix}/{name}",
        git(clone.source, "rev-parse", "HEAD"),
    )
    if kind == "branch":
        git(clone.upstream, "symbolic-ref", "HEAD", f"refs/heads/{name}")
    result = check_repo(clone.repo, saved)
    assert result.error == f"invalid-{kind}-name"
    assert not marker.exists()
    if kind == "branch":
        clone.repo.metadata["default_branch"] = name
        assert check_repo(clone.repo, saved).error == "invalid-branch-name"


def test_update_pins_tip_and_rechecks_after_prompt(clone, saved):
    check_repo(clone.repo, saved)
    checked = clone.commit()
    check_repo(clone.repo, saved)
    snap = snapshot(clone, saved)
    assert f"git merge --ff-only --no-autostash -- {checked}" in update_repo(
        clone.repo, snap, clone.config(), dry_run=True
    )
    clone.commit(content="later")
    assert "remote tip changed since check; run check again" in update_repo(
        clone.repo, snap, clone.config(), yes=True
    )
    check_repo(clone.repo, saved)

    def approve(_):
        (clone.path / "dirty").write_text("untracked")
        return "y"

    assert "not clean" in update_repo(
        clone.repo, snapshot(clone, saved), clone.config(), stdin=TTY(), approve=approve
    )


def test_untracked_setting_cannot_hide_dirt(clone, saved):
    check_repo(clone.repo, saved)
    clone.commit()
    check_repo(clone.repo, saved)
    git(clone.path, "config", "status.showUntrackedFiles", "no")
    (clone.path / "untracked").write_text("keep")
    assert "not clean" in update_repo(clone.repo, snapshot(clone, saved), clone.config(), yes=True)
    assert (
        update_repo(
            clone.repo, snapshot(clone, saved), clone.config(), yes=True, allow_untracked=True
        )
        == "updated"
    )


def test_pending_force_push_survives_checks(clone, saved):
    check_repo(clone.repo, saved)
    clone.commit()
    check_repo(clone.repo, saved)
    git(clone.source, "reset", "--hard", "HEAD~1")
    (clone.source / "file.txt").write_text("rewritten")
    git(clone.source, "add", ".")
    git(clone.source, "commit", "-m", "rewritten")
    git(clone.source, "push", "--force", "origin", "main")
    yesterday = datetime.now(UTC) - timedelta(days=1)
    first = check_repo(clone.repo, saved, now=yesterday)
    second = check_repo(clone.repo, saved)
    assert (
        next(f for f in second.flags if f.severity == "high").first_seen
        == yesterday.date().isoformat()
    )
    assert first.highest == second.highest == "high"
    assert exit_code([second]) == 20
    snap = snapshot(clone, saved)
    assert len([f for f in snap["pending_flags"] if f["severity"] == "high"]) == 1
    assert "first seen" in report.render([second])
    assert "high flag" in update_repo(clone.repo, snap, clone.config(), yes=True)
    assert "high flag" in update_repo(clone.repo, snap, clone.config(auto_update=["acme/widget"]))


def test_success_clears_pending_and_local_catchup(clone, saved):
    check_repo(clone.repo, saved)
    clone.commit("CODEOWNERS", "* @acme\n")
    check_repo(clone.repo, saved)
    snap = snapshot(clone, saved)
    assert snap["pending_flags"]
    assert update_repo(clone.repo, snap, clone.config(), yes=True) == "updated"
    assert snap["pending_flags"] == []
    assert check_repo(clone.repo, saved).highest == "none"


def test_ownership_rename(clone, saved):
    clone.commit("CODEOWNERS", "* @acme\n")
    check_repo(clone.repo, saved)
    git(clone.source, "mv", "CODEOWNERS", "notes.txt")
    git(clone.source, "commit", "-m", "rename owners")
    git(clone.source, "push", "origin", "main")
    assert any(
        "ownership files changed: CODEOWNERS" == f.reason
        for f in check_repo(clone.repo, saved).flags
    )


@pytest.mark.parametrize("changed", [False, True])
def test_tag_tombstones(clone, saved, changed):
    clone.tag("v1")
    check_repo(clone.repo, saved)
    old = git(clone.source, "rev-parse", "refs/tags/v1")
    git(clone.source, "push", "origin", ":refs/tags/v1")
    check_repo(clone.repo, saved)
    assert snapshot(clone, saved)["deleted_tags"] == {"v1": old}
    clone.commit()
    if changed:
        git(clone.source, "tag", "-f", "v1")
    git(clone.source, "push", "origin", "refs/tags/v1")
    result = check_repo(clone.repo, saved)
    assert any("tag recreated at a different commit" in f.reason for f in result.flags) == changed
    assert snapshot(clone, saved)["deleted_tags"] == {"v1": old}
    assert (
        update_repo(clone.repo, snapshot(clone, saved), clone.config(), yes=True, ignore_flags=True)
        == "updated"
    )
    clone.commit(content="ordinary later change")
    assert check_repo(clone.repo, saved).highest == "none"


def test_discovery_root_symlink_and_removed_candidate(clone, tmp_path, capsys):
    child = clone.path / "child"
    child.mkdir()
    config = clone.config()
    config.repos = [str(child)]
    assert discover(config)[0].reason == "not-a-repo-root"
    link = tmp_path / "parent-link"
    link.symlink_to(clone.path.parent, target_is_directory=True)
    config.repos = [str(link / clone.path.name)]
    assert discover(config)[0].reason == "symlink"
    write_private(config_path(), dump(clone.config()))
    assert cli.main(["check", "--stdout"]) == 0
    config.repos = []
    write_private(config_path(), dump(config))
    assert cli.main(["update", "--yes"]) == 0
    assert "updated" not in capsys.readouterr().out


def test_expiry_and_report_collisions(clone, saved, tmp_path):
    check_repo(clone.repo, saved)
    snap = snapshot(clone, saved)
    snap["checked_at"] = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    saved["api"] = {
        "https://api.github.com/repos/acme/widget": {},
        "https://api.github.com/repos/acme/widget/releases?per_page=10": {},
        "https://api.github.com/repos/acme/widget-other": {},
    }
    state.expire(saved, set(), datetime.now(UTC))
    assert not saved["repos"]
    assert list(saved["api"]) == ["https://api.github.com/repos/acme/widget-other"]
    destination = tmp_path / "2026-10-20-120000.md"
    assert state.write_report(destination, b"one") == destination
    assert state.write_report(destination, b"two").name.endswith("-2.md")
    assert state.write_report(destination, b"three").name.endswith("-3.md")
    assert destination.read_bytes() == b"one"


@pytest.mark.parametrize(
    "line,expected",
    [
        ("curl url | sh", True),
        ("wget url | bash", True),
        ("bash <(curl url)", True),
        ("sh <(wget url)", True),
        ('sh -c "$(curl url)"', True),
        ('bash -c "$(wget url)"', True),
        ("curl url | sudo bash", True),
        ("curl url | sudo -E sh", True),
        ("wget url | sudo sh", True),
        ("iwr url | iex", True),
        ("  # curl url | sh", False),
        ("# bash <(curl url)", False),
        ("curl url > download", False),
        ("echo shell", False),
        ("cat notes | sh", False),
    ],
)
def test_download_to_shell_table(line, expected):
    assert download_to_shell(line) is expected


def test_incomplete_prevents_auto_and_requires_prompt(clone, saved):
    check_repo(clone.repo, saved)
    clone.commit()
    snapshot(clone, saved)["tip"] = "a" * 40
    result = check_repo(clone.repo, saved)
    snap = snapshot(clone, saved)
    assert snap["complete"] is False
    assert result.highest == "review"
    assert "analysis incomplete" in update_repo(
        clone.repo, snap, clone.config(auto_update=["acme/widget"]), yes=True
    )
    assert "approval required" in update_repo(
        clone.repo, snap, clone.config(), yes=True, stdin=io.StringIO()
    )
    assert update_repo(clone.repo, snap, clone.config(), yes=True, stdin=TTY("y")) == "updated"


def test_truncated_tags_and_diff(clone, saved, monkeypatch):
    from upstream_watch import check, flags

    check_repo(clone.repo, saved)
    clone.commit(content="curl url | sh\n" * 20)
    clone.tag("v1")
    clone.tag("v2")
    monkeypatch.setattr(check, "TAG_LIMIT", 1)
    monkeypatch.setattr(flags, "DIFF_LIMIT", 32)
    check_repo(clone.repo, saved)
    snap = snapshot(clone, saved)
    assert not snap["complete"]
    assert set(snap["incomplete_reasons"]) == {"tag list truncated", "diff truncated"}


def test_api_sanitizes_decoded_content_and_allowlist(fake_api):
    routes, requests, base = fake_api
    token = 'a"b\\c'
    routes["/repos/acme/widget"] = (
        200,
        {"full_name": token, "extra": "drop", "owner": {"login": token, "email": "do not store"}},
        {"ETag": token},
    )
    cache = {}
    api = GitHub(cache, token=token, base=base)
    api.metadata("acme/widget")
    stored = next(iter(cache.values()))
    assert stored == {
        "etag": "[redacted]",
        "data": {"full_name": "[redacted]", "owner": {"login": "[redacted]"}},
    }
    assert "Authorization" not in requests[0][1]


def test_loopback_guard():
    with pytest.raises(OSError, match="non-loopback"):
        socket.create_connection(("192.0.2.1", 443))
    with socket.socket() as sock, pytest.raises(OSError, match="non-loopback"):
        sock.connect(("example.org", 443))


def test_bounds_and_markdown():
    assert escape("a-b.c~d (test)") == "a-b.c~d (test)"
    for value in ("# title", "- item", "+ item", "> quote"):
        assert escape(value).startswith("\\")
    assert escape("[x]|<y>*z*_q_`x`\\") == r"\[x\]\|\<y\>\*z\*\_q\_`x`\\".replace("`", r"\`")
    assert escape("1. item") == r"1\. item"
    body = release_body("x" * 5000)
    assert body == "x" * 300 + "…"
    assert len(release_body("\n".join("x" * 300 for _ in range(30)))) == 4001


def test_lock_excludes_second_process(clone, capsys):
    import os
    import subprocess
    import sys

    with state.lock():
        for command in ("check", "update"):
            result = subprocess.run(
                [sys.executable, "-m", "upstream_watch", command],
                capture_output=True,
                text=True,
                env=dict(os.environ),
                check=False,
            )
            assert result.returncode == 0
            assert result.stdout.strip() == "another upstream-watch run is in progress"
    assert not state_path().exists()


@pytest.mark.parametrize(
    "exception",
    [
        http.client.HTTPException,
        http.client.RemoteDisconnected,
        socket.timeout,
        OSError,
    ],
)
def test_api_read_failures_redacted(exception):
    token = "secret-runtime-value"

    class Response:
        code = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            raise exception(token)

    class Opener:
        def open(self, request, **kwargs):
            assert request.get_header("Authorization") == f"Bearer {token}"
            return Response()

    api = GitHub({}, token=token, opener=Opener())
    assert api.metadata("acme/widget") == {}
    assert token not in str(api.messages)
    assert "unavailable" in api.messages[0]


def test_api_body_limit_and_cached_fields(fake_api):
    routes, _, base = fake_api
    route = "/repos/acme/widget/releases?per_page=10"
    routes[route] = (200, [{"body": "x" * (1024 * 1024)}], {})
    api = GitHub({}, base=base)
    assert api.releases("acme/widget") == []
    assert api.cache == {}
    assert "exceeds 1 MiB" in api.messages[0]
    routes[route] = (200, [{"body": "a" * 500, "tag_name": "v1", "author": {"email": "omit"}}], {})
    assert api.releases("acme/widget")[0]["body"] == "a" * 300 + "…"
    assert "email" not in str(api.cache)


def test_cli_redacts_runtime_token_on_errors(monkeypatch, capsys):
    token = "runtime-secret-value"
    monkeypatch.setattr(cli, "runtime_token", lambda: token)

    def fail(*args):
        raise OSError(token)

    monkeypatch.setattr(cli, "execute", fail)
    assert cli.main(["check"]) == 1
    output = capsys.readouterr()
    assert token not in output.out + output.err
    assert "[redacted]" in output.err


def test_auto_update_failure_exit_and_line(clone, monkeypatch, capsys):
    write_private(config_path(), dump(clone.config(auto_update=["acme/widget"])))
    assert cli.main(["check", "--stdout"]) == 0
    clone.commit()
    monkeypatch.setattr(cli, "update_repo", lambda *a, **k: "update failed: fixture failure")
    assert cli.main(["check", "--stdout", "--update-allowed"]) == 1
    assert "acme/widget: update failed: fixture failure" in capsys.readouterr().out.splitlines()


def test_scheduler_absolute_config_and_symlink_path(tmp_path, monkeypatch):
    import plistlib
    import shutil

    from upstream_watch.schedule import render, scheduler_path

    binaries = tmp_path / "wrappers"
    binaries.mkdir()
    (binaries / "git").symlink_to("/usr/bin/git")
    monkeypatch.setattr(shutil, "which", lambda name: str(binaries / name))
    assert scheduler_path(tmp_path, "launchd").split(":")[0] == str(binaries)
    data = plistlib.loads(next(iter(render(tmp_path, Path("relative.toml")).values())))
    assert data["ProgramArguments"][4] == str(Path("relative.toml").absolute())


def test_cli_incomplete_auto_skip_and_failed_check_marker(clone, monkeypatch, capsys):
    write_private(config_path(), dump(clone.config(auto_update=["acme/widget"])))
    assert cli.main(["check", "--stdout"]) == 0
    clone.commit()
    saved = state.load(state_path())
    saved["repos"][clone.repo.key]["tip"] = "a" * 40
    state.save(state_path(), saved)
    before = git(clone.path, "rev-parse", "HEAD")
    assert cli.main(["check", "--stdout", "--update-allowed"]) == 10
    assert git(clone.path, "rev-parse", "HEAD") == before
    assert "skipped: analysis incomplete" in capsys.readouterr().out
    original = gitutil.run

    def fail(path, *args, **kwargs):
        if args[0] == "fetch":
            raise gitutil.GitError("fixture fetch failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(gitutil, "run", fail)
    assert cli.main(["check", "--stdout"]) == 2
    assert cli.main(["update", "--yes"]) == 10
    assert "last check failed" in capsys.readouterr().out


def test_invalid_names_and_remote_shas(clone, saved, monkeypatch):
    for name, kind in [
        ("bad\nbranch", "branch"),
        ("part/-bad", "tag"),
        ("-bad", "remote"),
        ("bad branch", "branch"),
    ]:
        with pytest.raises(gitutil.GitError, match=f"invalid-{kind}-name"):
            gitutil.validate(clone.path, name, kind)
    original = gitutil.run

    def malformed(path, *args, **kwargs):
        if args[:2] == ("ls-remote", "--tags"):
            return subprocess.CompletedProcess(args, 0, "not-a-sha\trefs/tags/v1\n", "")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(gitutil, "run", malformed)
    assert check_repo(clone.repo, saved).error == "invalid-remote-sha"


def test_cached_data_migration_removes_payloads_and_emails():
    cache = {
        "https://api.github.com/repos/acme/widget": {
            "etag": "secret",
            "data": {"owner": {"login": "acme", "email": "omit"}},
        },
        "https://api.github.com/repos/acme/widget/git/tags/" + "a" * 40: {
            "data": {
                "verification": {"verified": True, "reason": "secret", "signature": "omit"},
                "tagger": {"email": "omit"},
            }
        },
        "https://api.github.com/repos/acme/widget/git/ref/tags/v1": {
            "data": {"object": {"sha": "a" * 40}}
        },
    }
    GitHub(cache, token="secret")
    assert "omit" not in str(cache)
    assert "secret" not in str(cache)
    assert len(cache) == 2


def test_api_base_environment_ignored(monkeypatch):
    monkeypatch.setenv("UPSTREAM_WATCH_API_URL", "https://example.org")
    assert GitHub({}).base == "https://api.github.com"


def test_commit_subject_bounds(clone):
    from upstream_watch.changelog import summarize

    old = git(clone.source, "rev-parse", "HEAD")
    git(clone.source, "commit", "--allow-empty", "-m", "feat: " + "x" * 500)
    result = summarize(clone.source, f"{old}..HEAD")
    assert result["groups"]["feat"] == ["feat: " + "x" * 194 + "…"]


def test_exit_code_precedence(clone):
    from upstream_watch.check import Result
    from upstream_watch.flags import Flag

    failed = Result(clone.repo, status="fetch failed", error="offline")
    high = Result(clone.repo, pending=[Flag("high", "moved")], update="update failed: error")
    review = Result(clone.repo, pending=[Flag("review", "owners")])
    assert exit_code([failed]) == 2
    assert exit_code([failed, high, review]) == 20
    assert exit_code([failed, review]) == 10
    assert exit_code([failed, Result(clone.repo)]) == 1


def test_argument_errors_are_redacted(monkeypatch, capsys):
    token = "runtime-secret-value"
    monkeypatch.setattr(cli, "runtime_token", lambda: token)
    assert cli.main(["unknown-" + token]) == 1
    output = capsys.readouterr()
    assert token not in output.out + output.err
    assert "[redacted]" in output.err
