import io
from datetime import UTC, datetime, timedelta

import pytest

from upstream_watch import gitutil
from upstream_watch.check import check_repo
from upstream_watch.update import refusal, update_repo

from .conftest import git


@pytest.fixture
def checked(clone, saved):
    check_repo(clone.repo, saved)
    clone.commit()
    check_repo(clone.repo, saved)
    return saved["repos"][clone.repo.key]


def test_success_uses_ff_only(clone, checked, monkeypatch):
    calls = []
    original = gitutil.run

    def record(path, *args, **kwargs):
        calls.append(args)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(gitutil, "run", record)
    assert update_repo(clone.repo, checked, clone.config(), yes=True) == "updated"
    assert ("merge", "--ff-only", "--no-autostash", "--", checked["tip"]) in calls
    assert not any(args[0] in {"fetch", "pull"} for args in calls)
    assert (clone.path / "file.txt").read_text() == "change\n"


@pytest.mark.parametrize(
    "change,expected",
    [
        ("dirty", "not clean"),
        ("index", "not clean"),
        ("untracked", "not clean"),
        ("ahead", "ahead"),
        ("detached", "detached"),
        ("tracking", "does not track"),
        ("merge", "in progress"),
        ("rebase", "in progress"),
        ("cherry", "in progress"),
        ("bisect", "in progress"),
        ("stale", "stale"),
        ("future", "stale"),
        ("invalid_time", "timestamp"),
        ("tip", "tip changed"),
        ("high", "high flag"),
    ],
)
def test_refusals(clone, checked, change, expected):
    if change in {"dirty", "index"}:
        (clone.path / "file.txt").write_text("dirty")
        if change == "index":
            git(clone.path, "add", "file.txt")
    elif change == "untracked":
        (clone.path / "extra").write_text("extra")
    elif change == "ahead":
        git(clone.path, "commit", "--allow-empty", "-m", "local")
    elif change == "detached":
        git(clone.path, "checkout", "--detach")
    elif change == "tracking":
        git(clone.path, "branch", "--unset-upstream")
    elif change in {"merge", "rebase", "cherry", "bisect"}:
        name = {
            "merge": "MERGE_HEAD",
            "rebase": "rebase-merge",
            "cherry": "CHERRY_PICK_HEAD",
            "bisect": "BISECT_LOG",
        }[change]
        (clone.path / ".git" / name).write_text("marker")
    elif change in {"stale", "future"}:
        checked["checked_at"] = (
            datetime.now(UTC) + timedelta(days=-2 if change == "stale" else 1)
        ).isoformat()
    elif change == "invalid_time":
        checked["checked_at"] = "invalid"
    elif change == "tip":
        checked["tip"] = "b" * 40
    elif change == "high":
        checked["pending_flags"] = [{"severity": "high", "reason": "force-push"}]
    before = git(clone.path, "rev-parse", "HEAD")
    assert expected in update_repo(clone.repo, checked, clone.config(), yes=True)
    assert git(clone.path, "rev-parse", "HEAD") == before


def test_untracked_opt_in_and_dry_run(clone, checked):
    (clone.path / "extra").write_text("keep")
    before = git(clone.path, "rev-parse", "HEAD")
    assert "dry-run: 1 commits" in update_repo(
        clone.repo, checked, clone.config(), dry_run=True, allow_untracked=True
    )
    assert git(clone.path, "rev-parse", "HEAD") == before
    assert (
        update_repo(clone.repo, checked, clone.config(), yes=True, allow_untracked=True)
        == "updated"
    )
    assert (clone.path / "extra").read_text() == "keep"


def test_ignore_high_still_prints(clone, checked, capsys):
    checked["pending_flags"] = [{"severity": "high", "reason": "force-push"}]
    assert (
        update_repo(clone.repo, checked, clone.config(), yes=True, ignore_flags=True) == "updated"
    )
    assert "high: force-push" in capsys.readouterr().out


class TTY(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize(
    "answer,expected", [("y", "updated"), ("YES", "updated"), ("n", "declined"), ("", "declined")]
)
def test_interactive(clone, checked, answer, expected):
    assert update_repo(clone.repo, checked, clone.config(), stdin=TTY(answer)) == expected


def test_approval_callback_and_non_tty(clone, checked):
    assert "non-TTY" in update_repo(clone.repo, checked, clone.config(), stdin=io.StringIO())
    questions = []

    def approve(question):
        questions.append(question)
        return "y"

    assert (
        update_repo(clone.repo, checked, clone.config(), stdin=TTY(), approve=approve) == "updated"
    )
    assert "1 commits" in questions[0]


@pytest.mark.parametrize("by_path", [True, False])
def test_auto_update(clone, checked, by_path):
    config = clone.config(auto_update=[str(clone.path) if by_path else "acme/widget"])
    assert update_repo(clone.repo, checked, config, stdin=io.StringIO()) == "updated"


def test_auto_block_and_only(clone, checked):
    assert update_repo(clone.repo, checked, clone.config(), auto_only=True) == "not auto-approved"
    checked["pending_flags"] = [{"severity": "review", "reason": "new identity"}]
    config = clone.config(auto_update=["acme/widget"], auto_update_blocked_by=["review"])
    assert "blocked by flags" in update_repo(clone.repo, checked, config)
    assert refusal(clone.repo, None) == ("no successful check", 0)


@pytest.mark.parametrize("source", ["snapshot", "pending", "both"])
@pytest.mark.parametrize("auto_only", [True, False])
def test_pending_incomplete_overrides_snapshot_and_auto_policy(clone, checked, source, auto_only):
    checked["complete"] = source == "pending"
    checked["incomplete_reasons"] = [] if source == "pending" else ["missing range"]
    checked["pending_flags"] = (
        [{"severity": "review", "reason": "analysis incomplete: missing range", "scope": "repo"}]
        if source != "snapshot"
        else []
    )
    config = clone.config(auto_update=["acme/widget"], auto_update_blocked_by=[])
    assert update_repo(clone.repo, checked, config, yes=True, auto_only=auto_only) == (
        "skipped: analysis incomplete: missing range"
    )
    assert update_repo(clone.repo, checked, clone.config(), yes=True, stdin=TTY("n")) == "declined"
    assert (
        update_repo(clone.repo, checked, clone.config(), yes=True, stdin=io.StringIO())
        == "skipped: approval required (non-TTY)"
    )


def test_divergent_pull_failure_untouched(clone, checked):
    # A rewritten upstream must be refused without fetching or changing HEAD.
    git(clone.source, "checkout", "--orphan", "replacement")
    git(clone.source, "add", ".")
    git(clone.source, "commit", "-m", "unrelated root")
    git(clone.source, "push", "--force", "origin", "HEAD:main")
    before = git(clone.path, "rev-parse", "HEAD")
    content = (clone.path / "file.txt").read_text()
    result = update_repo(clone.repo, checked, clone.config(), yes=True)
    assert "remote tip changed since check; run check again" in result
    assert git(clone.path, "rev-parse", "HEAD") == before
    assert (clone.path / "file.txt").read_text() == content


def test_remote_changed_since_check(clone, checked):
    git(clone.path, "remote", "set-url", "origin", "https://github.com/example-org/other")
    assert "remote URL changed" in update_repo(clone.repo, checked, clone.config(), yes=True)


def test_repository_store_replaced(clone, checked):
    clone.repo.common_dir += "-different"
    assert "store changed" in update_repo(clone.repo, checked, clone.config(), yes=True)


def test_recheck_after_interactive_approval(clone, checked):
    def approve(question):
        (clone.path / "file.txt").write_text("edit during approval")
        return "y"

    result = update_repo(clone.repo, checked, clone.config(), stdin=TTY(), approve=approve)
    assert "not clean" in result
    assert (clone.path / "file.txt").read_text() == "edit during approval"


@pytest.mark.parametrize(
    "config_field,value,reason", [("own", "ACME", "own"), ("exclude", "*clone-*", "excluded")]
)
def test_changed_config_can_exclude_checked_repo(clone, checked, config_field, value, reason):
    if config_field == "exclude":
        value = str(clone.path.parent / value)
    config = clone.config(**{config_field: [value]})
    assert update_repo(clone.repo, checked, config, yes=True) == "skipped: " + reason
