import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from upstream_watch import cli, gitutil, report, state
from upstream_watch.check import check_repo, exit_code
from upstream_watch.config import config_path, dump, state_path, write_private
from upstream_watch.github import GitHub

from .conftest import git


def golden_results(factory, saved, fake_api):
    routes, _, base = fake_api
    widget = factory()
    tool = factory("example-org/tool")
    api = GitHub(saved["api"], base=base)
    route = "/repos/acme/widget/releases?per_page=10"
    routes[route] = (200, [{"tag_name": "v1"}], {})
    check_repo(widget.repo, saved, api)
    check_repo(tool.repo, saved)
    widget.commit("install.sh", "curl https://example.org/tool | sh\n", "fix: install [guard]")
    widget.tag("v2")
    routes[route] = (
        200,
        [
            {
                "name": "Widget 2",
                "tag_name": "v2",
                "published_at": "2026-01-02",
                "prerelease": False,
                "body": "Fix install.\nhttps://example.org/notes",
            },
            {"tag_name": "v1"},
        ],
        {},
    )
    changed = check_repo(widget.repo, saved, api, now=datetime(2026, 1, 2, tzinfo=UTC))
    unchanged = check_repo(tool.repo, saved)
    changed.repo.path = Path.home() / "src/widget"
    unchanged.repo.path = Path.home() / "src/tool"
    return [changed, unchanged]


def test_golden(factory, saved, fake_api):
    results = golden_results(factory, saved, fake_api)
    content = report.render(results)
    assert content == (Path(__file__).parents[1] / "examples/report.md").read_text()
    assert "Unchanged: example-org/tool" in content


def test_report_errors_flags_and_exit(clone, saved):
    result = check_repo(clone.repo, saved)
    assert exit_code([]) == 0
    assert "baseline recorded" in report.render([result])
    result.status = "fetch failed"
    result.error = "fatal: example"
    assert "fatal: example" in report.render([result])
    assert exit_code([result]) == 2
    assert report.display_path(Path("/example/path")) == "/example/path"


def test_cli_init_list_check_report_and_update(clone, capsys, tmp_path):
    cfg = config_path()
    assert cli.main(["init", "--own", "me-example", "--root", str(clone.path)]) == 0
    assert cfg.stat().st_mode & 0o777 == 0o600
    assert cli.main(["init", "--own", "me-example"]) == 1
    assert cli.main(["init", "--own", "me-example", "--force"]) == 0
    write_private(cfg, dump(clone.config()))
    assert cli.main(["list"]) == 0
    assert "watch acme/widget" in capsys.readouterr().out
    destination = tmp_path / "report.md"
    assert cli.main(["check", "--report", str(destination)]) == 0
    assert "baseline recorded" in destination.read_text()
    assert destination.stat().st_mode & 0o777 == 0o600
    clone.commit("install.sh", "echo install\n", "build: install")
    assert cli.main(["check", "--stdout"]) == 10
    assert "install, build or CI" in capsys.readouterr().out
    assert cli.main(["update", str(clone.path), "--dry-run"]) == 10
    assert "dry-run:" in capsys.readouterr().out
    assert cli.main(["update", str(clone.path), "--yes"]) == 0
    assert (clone.path / "install.sh").exists()
    assert cli.main(["update", str(tmp_path / "missing"), "--yes"]) == 1
    assert cli.main(["check"]) == 0
    assert list((state_path().parent / "reports").glob("*.md"))


def test_cli_auto_and_api(clone, fake_api, capsys, monkeypatch):
    routes, requests, base = fake_api
    monkeypatch.setattr(cli, "GitHub", lambda cache, **kwargs: GitHub(cache, base=base, **kwargs))
    routes["/repos/acme/widget"] = (200, {"full_name": "acme/widget", "default_branch": "main"}, {})
    routes["/repos/acme/widget/releases?per_page=10"] = (200, [], {})
    config = clone.config(auto_update=["acme/widget"])
    config.github_api = True
    write_private(config_path(), dump(config))
    assert cli.main(["check", "--stdout"]) == 0
    assert len(requests) == 2
    clone.commit()
    assert cli.main(["check", "--update-allowed", "--stdout"]) == 0
    assert (clone.path / "file.txt").read_text() == "change\n"
    assert "updated" in capsys.readouterr().out


def test_cli_help_usage_schedule(capsys):
    assert cli.main(["--help"]) == 0
    assert "Exit codes:" in capsys.readouterr().out
    assert cli.main(["unknown"]) == 1
    assert cli.main(["--version"]) == 0
    assert cli.main(["schedule", "print", "--platform", "systemd"]) == 0
    assert "Persistent=true" in capsys.readouterr().out
    assert cli.main(["list"]) == 1


def test_init_suggests_does_not_save(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/example/gh")

    def run(argv, **kwargs):
        assert argv == ["gh", "api", "user", "--jq", ".login"]
        return subprocess.CompletedProcess(argv, 0, "me-example\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert cli.main(["init"]) == 0
    assert "own = []" in config_path().read_text()
    assert "Suggested own account: me-example" in capsys.readouterr().out

    def failure(*args, **kwargs):
        raise subprocess.TimeoutExpired("gh", 15)

    monkeypatch.setattr(subprocess, "run", failure)
    assert cli.main(["init", "--force"]) == 0


def test_git_subprocess_safety_and_failures(tmp_path, monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs)
        if "--show-toplevel" in argv:
            return subprocess.CompletedProcess(argv, 0, str(tmp_path), "")
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(gitutil.GitError, match="git command failed"):
        gitutil.run(tmp_path, "fetch", "origin")
    assert seen[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen[0]["env"]["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    monkeypatch.setenv("GIT_SSH_COMMAND", "custom-ssh")
    gitutil.run(tmp_path, "status", check=False)
    assert seen[-1]["env"]["GIT_SSH_COMMAND"] == "custom-ssh"

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 120)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(gitutil.GitError, match="timed out"):
        gitutil.run(tmp_path, "fetch", "origin")


def test_update_failure_cli(clone, saved, monkeypatch):
    check_repo(clone.repo, saved)
    write_private(config_path(), dump(clone.config()))
    state.save(state_path(), saved)
    monkeypatch.setattr(cli, "update_repo", lambda *a, **k: "update failed: simulated")
    assert cli.main(["update"]) == 1


def test_report_secret_and_untrusted_markdown(clone, saved, fake_api):
    routes, _, base = fake_api
    token = "gh" + "p_" + "z" * 36
    api = GitHub(saved["api"], token=token, base=base)
    routes["/repos/acme/widget/releases?per_page=10"] = (200, [], {})
    check_repo(clone.repo, saved, api)
    routes["/repos/acme/widget/releases?per_page=10"] = (
        200,
        [{"tag_name": "v2", "body": token + "\n<script>\n[link](https://example.com)"}],
        {},
    )
    result = check_repo(clone.repo, saved, api)
    content = report.render([result])
    assert token not in content + json.dumps(saved)
    assert "<script>" not in content
    assert "\\[link\\]" in content


def test_token_echoed_by_git_is_not_persisted_or_reported(clone, saved, fake_api):
    routes, _, base = fake_api
    token = "gh" + "p_" + "X" * 36
    api = GitHub(saved["api"], token=token, base=base)
    routes["/repos/acme/widget/releases?per_page=10"] = (200, [], {})
    check_repo(clone.repo, saved, api)
    clone.commit("install-" + token + ".sh", "echo install\n", "fix: " + token)
    result = check_repo(clone.repo, saved, api)
    assert token not in report.render([result]) + json.dumps(saved)


def test_moved_tag_blocks_update_with_pending_high(clone, capsys):
    write_private(config_path(), dump(clone.config()))
    clone.tag("v1")
    assert cli.main(["check", "--stdout"]) == 0
    previous = state.load(state_path())["repos"]
    clone.commit()
    git(clone.source, "tag", "-f", "v1")
    git(clone.source, "push", "--force", "origin", "refs/tags/v1")
    assert cli.main(["check", "--stdout"]) == 20
    assert state.load(state_path())["repos"] != previous
    assert cli.main(["update", "--yes"]) == 20
    assert "high flag" in capsys.readouterr().out


def test_module_entry_point(monkeypatch, capsys):
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["upstream-watch", "--help"])
    with pytest.raises(SystemExit) as error:
        runpy.run_module("upstream_watch", run_name="__main__")
    assert error.value.code == 0
    assert "Exit codes" in capsys.readouterr().out


def test_stdout_stays_markdown_during_auto_update(clone, capsys):
    write_private(config_path(), dump(clone.config(auto_update=["acme/widget"])))
    assert cli.main(["check", "--stdout"]) == 0
    capsys.readouterr()
    clone.commit("install.sh", "echo install\n", "build: installer")
    assert cli.main(["check", "--stdout", "--update-allowed"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("# Upstream watch\n")
    assert "| none | updated |" in output
    assert state.load(state_path())["repos"][clone.repo.key]["pending_flags"] == []
    assert "**review**" in output
