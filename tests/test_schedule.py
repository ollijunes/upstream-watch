import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from upstream_watch.config import WatchError
from upstream_watch.schedule import LABEL, render, schedule, scheduler_path, unit_quote


@pytest.mark.parametrize("weekday", [None, 1, 7])
def test_plist(tmp_path, weekday):
    files = render(tmp_path, tmp_path / "config.toml", "09:31", weekday, True)
    path, content = next(iter(files.items()))
    data = plistlib.loads(content)
    assert path.name == LABEL + ".plist"
    assert data["ProgramArguments"][-1] == "--update-allowed"
    assert data["StartCalendarInterval"]["Hour"] == 9
    if weekday:
        assert data["StartCalendarInterval"]["Weekday"] == weekday % 7
    assert data["ProcessType"] == "Background"
    assert data["LowPriorityIO"] is True and data["Nice"] == 10
    assert data["RunAtLoad"] is False and "KeepAlive" not in data
    assert str(tmp_path / ".local/bin") in data["EnvironmentVariables"]["PATH"]
    if sys.platform == "darwin":
        target = tmp_path / "test.plist"
        target.write_bytes(content)
        subprocess.run(["plutil", "-lint", str(target)], check=True, capture_output=True)


def test_systemd(tmp_path):
    files = render(tmp_path, tmp_path / "config.toml", "09:31", 2, True, "systemd")
    text = b"\n".join(files.values()).decode()
    assert "OnCalendar=Tue *-*-* 09:31:00" in text
    assert "Persistent=true" in text
    assert "SuccessExitStatus=10 20" in text
    assert "--update-allowed" in text
    binary = shutil.which("systemd-analyze")
    if binary:
        targets = []
        for path, data in files.items():
            target = tmp_path / path.name
            target.write_bytes(data)
            targets.append(str(target))
        verified = subprocess.run(
            [binary, "--user", "verify", *targets], capture_output=True, text=True
        )
        assert verified.returncode == 0, verified.stdout + verified.stderr
    daily = render(tmp_path, tmp_path / "config.toml", platform="systemd")
    assert b"OnCalendar=*-*-*" in list(daily.values())[1]


@pytest.mark.parametrize(
    "kwargs", [{"at": "25:00"}, {"at": "1:00"}, {"weekday": 0}, {"platform": "cron"}]
)
def test_invalid_scheduler(tmp_path, kwargs):
    with pytest.raises(WatchError):
        render(tmp_path, tmp_path / "config.toml", **kwargs)


def test_scheduler_path_and_quoting(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: str(tmp_path / "bin" / name))
    assert scheduler_path(tmp_path, "launchd").split(":").count(str(tmp_path / "bin")) == 1
    assert unit_quote('a%$"\\') == '"a%%$$\\"\\\\"'
    with pytest.raises(WatchError):
        unit_quote("bad\npath")


def test_install_uninstall_mocked(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 1 if "bootout" in argv else 0)

    monkeypatch.setattr(subprocess, "run", run)
    config = tmp_path / "config.toml"
    assert schedule("install", tmp_path, config, dry_run=True) == 0
    assert not commands
    assert "launchctl bootstrap" in capsys.readouterr().out
    assert schedule("install", tmp_path, config) == 0
    assert [c[:2] for c in commands] == [
        ["plutil", "-lint"],
        ["launchctl", "bootout"],
        ["launchctl", "bootstrap"],
    ]
    path = tmp_path / f"Library/LaunchAgents/{LABEL}.plist"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(WatchError, match="bootout"):
        schedule("uninstall", tmp_path, config)
    assert path.exists()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1 if "print" in argv else 0),
    )
    assert schedule("uninstall", tmp_path, config) == 0
    assert not path.exists()
    assert schedule("print", tmp_path, config, platform="systemd") == 0
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(WatchError):
        schedule("install", tmp_path, config)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1)
    )
    with pytest.raises(WatchError, match="plutil"):
        schedule("install", tmp_path, config)


def test_shipped_templates_match_renderer(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/git" if name == "git" else "/usr/local/bin/gh"
    )
    root = Path(__file__).parents[1] / "contrib"
    for platform in ("launchd", "systemd"):
        for path, content in render(
            Path("/example/home"),
            Path("/example/home/.config/upstream-watch/config.toml"),
            platform=platform,
        ).items():
            assert content == (root / platform / path.name).read_bytes()
