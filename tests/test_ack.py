import copy
import io

import pytest

from upstream_watch import cli, gitutil, state
from upstream_watch.check import check_repo
from upstream_watch.config import config_path, dump, state_path, write_private

from .conftest import git
from .test_update import TTY


@pytest.fixture
def pending(clone, saved):
    check_repo(clone.repo, saved)
    snapshot = saved["repos"][clone.repo.key]
    snapshot.update(
        complete=False,
        incomplete_reasons=["missing range"],
        pending_flags=[
            {
                "severity": "review",
                "reason": "analysis incomplete: missing range",
                "scope": "repo",
                "first_seen": "2026-01-02",
            },
            {"severity": "high", "reason": "tag moved: v1", "scope": "repo"},
        ],
    )
    write_private(config_path(), dump(clone.config()))
    state.save(state_path(), saved)
    return saved


@pytest.mark.parametrize(
    "yes,stream,cleared,message",
    [
        (False, io.StringIO(), False, "skipped: acknowledgement required (non-TTY)"),
        (False, TTY(""), False, "declined"),
        (False, TTY("n\n"), False, "declined"),
        (False, TTY("y\n"), True, "acknowledged"),
        (False, TTY("YES\n"), True, "acknowledged"),
        (True, io.StringIO(), True, "acknowledged"),
    ],
)
def test_ack_confirmation_state_only(
    clone, pending, monkeypatch, capsys, yes, stream, cleared, message
):
    before = state_path().read_bytes()
    head = git(clone.path, "rev-parse", "HEAD")
    files = {
        p.relative_to(clone.path): p.read_bytes() for p in clone.path.rglob("*") if p.is_file()
    }

    def unexpected(*args, **kwargs):
        pytest.fail("ack must not invoke Git or the GitHub client")

    monkeypatch.setattr(gitutil, "run", unexpected)
    monkeypatch.setattr(cli, "GitHub", unexpected)
    monkeypatch.setattr(cli.sys, "stdin", stream)
    assert cli.main(["ack", *([] if not yes else ["--yes"])]) == 0
    output = capsys.readouterr().out
    assert "- review: analysis incomplete: missing range (first seen 2026-01-02)" in output
    date = pending["repos"][clone.repo.key]["checked_at"][:10]
    assert f"- high: tag moved: v1 (first seen {date})" in output
    assert f"acme/widget: {message}" in output
    assert ("acknowledge pending flags? [y/N] " in output) == (not yes and stream.isatty())
    if cleared:
        expected = copy.deepcopy(pending)
        expected["repos"][clone.repo.key].update(
            pending_flags=[], complete=True, incomplete_reasons=[]
        )
        assert state.load(state_path()) == expected
    else:
        assert state_path().read_bytes() == before
    assert {
        p.relative_to(clone.path): p.read_bytes() for p in clone.path.rglob("*") if p.is_file()
    } == files
    assert git(clone.path, "rev-parse", "HEAD") == head


def test_ack_paths_and_per_repository_prompt(clone, factory, pending, monkeypatch, capsys):
    second = factory("example/tool")
    check_repo(second.repo, pending)
    pending["repos"][second.repo.key]["pending_flags"] = [
        {"severity": "review", "reason": "legacy flag", "first_seen": "2026-01-01"}
    ]
    config = clone.config()
    config.repos.append(str(second.path))
    write_private(config_path(), dump(config))
    state.save(state_path(), pending)
    monkeypatch.setattr(cli.sys, "stdin", TTY("n\ny\n"))
    assert cli.main(["ack", str(clone.path), str(second.path)]) == 0
    output = capsys.readouterr().out
    assert output.count("acknowledge pending flags? [y/N] ") == 2
    saved = state.load(state_path())
    assert saved["repos"][clone.repo.key] == pending["repos"][clone.repo.key]
    assert saved["repos"][second.repo.key]["pending_flags"] == []
    assert cli.main(["ack", str(second.path), "--yes"]) == 0
    assert "no pending flags" in capsys.readouterr().out
    assert state.load(state_path()) == saved
    assert cli.main(["ack", str(clone.path), "--yes"]) == 0
    assert state.load(state_path())["repos"][clone.repo.key]["pending_flags"] == []


@pytest.mark.parametrize("restriction", ["own", "exclude", "unwatched", "missing", "symlink"])
def test_ack_rejects_unwatched_paths_without_changing_state(
    clone, pending, tmp_path, restriction, capsys
):
    config = clone.config()
    path = clone.path
    if restriction == "own":
        config.own = ["ACME"]
    elif restriction == "exclude":
        config.exclude = [str(clone.path)]
    elif restriction == "unwatched":
        config.repos = []
    elif restriction == "missing":
        path = tmp_path / "missing"
    else:
        path = tmp_path / "link"
        path.symlink_to(clone.path, target_is_directory=True)
    write_private(config_path(), dump(config))
    before = state_path().read_bytes()
    assert cli.main(["ack", str(path), "--yes"]) == 1
    assert (
        "symlink"
        if restriction == "symlink"
        else "Requested path has no saved state in current configuration"
    ) in capsys.readouterr().err
    assert state_path().read_bytes() == before


def test_ack_legacy_flags_and_later_check(clone, pending, monkeypatch, capsys):
    snapshot = pending["repos"][clone.repo.key]
    snapshot["flags"] = snapshot.pop("pending_flags")
    state.save(state_path(), pending)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO())
    assert cli.main(["ack", "--yes"]) == 0
    assert "first seen 2026-01-02" in capsys.readouterr().out
    saved = state.load(state_path())
    assert check_repo(clone.repo, saved).highest == "none"
    assert saved["repos"][clone.repo.key]["complete"] is True
