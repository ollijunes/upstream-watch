"""launchd installation and portable user scheduler templates."""

import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .config import WatchError, no_symlinks, private_dir, write_private

LABEL = "com.github.ollijunes.upstream-watch"


def scheduler_path(home: Path, platform: str) -> str:
    directories = []
    for program in ("git", "gh"):
        binary = shutil.which(program)
        if binary:
            directories.append(str(Path(binary).parent))
    directories.append(str(home / ".local/bin"))
    defaults = (
        "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        if platform == "launchd"
        else "/usr/local/bin:/usr/bin:/bin"
    )
    return ":".join(dict.fromkeys([*directories, *defaults.split(":")]))


def unit_quote(value: str) -> str:
    if any(ord(c) < 32 for c in value):
        raise WatchError("Scheduler paths cannot contain control characters")
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


def render(
    home: Path,
    config: Path,
    at: str = "08:00",
    weekday: int | None = None,
    update_allowed: bool = False,
    platform: str = "launchd",
) -> dict[Path, bytes]:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
        raise WatchError("--at must be HH:MM (00:00 through 23:59)")
    if weekday is not None and weekday not in range(1, 8):
        raise WatchError("--weekday must be 1 (Monday) through 7 (Sunday)")
    if platform not in {"launchd", "systemd"}:
        raise WatchError("Unsupported scheduler platform")
    hour, minute = map(int, at.split(":"))
    argv = [
        str(Path(sys.executable).absolute()),
        "-m",
        "upstream_watch",
        "--config",
        str(config.expanduser().absolute()),
        "check",
    ]
    if update_allowed:
        argv.append("--update-allowed")
    env_path = scheduler_path(home, platform)
    if platform == "launchd":
        interval = {"Hour": hour, "Minute": minute}
        if weekday is not None:
            interval["Weekday"] = weekday % 7
        log = home / "Library/Logs/upstream-watch"
        data = {
            "Label": LABEL,
            "ProgramArguments": argv,
            "StartCalendarInterval": interval,
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "Nice": 10,
            "RunAtLoad": False,
            "EnvironmentVariables": {"PATH": env_path},
            "StandardOutPath": str(log / "stdout.log"),
            "StandardErrorPath": str(log / "stderr.log"),
        }
        content = b"\n".join(
            line for line in plistlib.dumps(data).split(b"\n") if not line.startswith(b"<!DOCTYPE")
        )
        return {home / f"Library/LaunchAgents/{LABEL}.plist": content}
    directory = home / ".config/systemd/user"
    service = (
        "[Unit]\nDescription=Check public upstream repositories\n\n[Service]\nType=oneshot\n"
        f"ExecStart={' '.join(unit_quote(arg) for arg in argv)}\n"
        f"Environment={unit_quote('PATH=' + env_path)}\nNice=10\nIOSchedulingClass=idle\n"
        "SuccessExitStatus=10 20\n"
    )
    day = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[weekday - 1] + " " if weekday else ""
    timer = (
        "[Unit]\nDescription=Scheduled upstream checks\n\n[Timer]\n"
        f"OnCalendar={day}*-*-* {at}:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
    )
    return {
        directory / "upstream-watch.service": service.encode(),
        directory / "upstream-watch.timer": timer.encode(),
    }


def schedule(
    action: str,
    home: Path,
    config: Path,
    *,
    at: str = "08:00",
    weekday: int | None = None,
    update_allowed: bool = False,
    platform: str = "launchd",
    dry_run: bool = False,
) -> int:
    files = render(home, config, at, weekday, update_allowed, platform)
    if action == "print":
        for path, content in files.items():
            print(f"# {path.name}\n{content.decode()}", end="")
        return 0
    if platform != "launchd" or sys.platform != "darwin":
        raise WatchError(
            "Schedule install/uninstall requires macOS launchd; "
            "use schedule print --platform systemd"
        )
    path = next(iter(files))
    no_symlinks(path)
    domain = f"gui/{os.getuid()}"
    installing = action == "install"
    commands = (
        [
            ["plutil", "-lint", str(path)],
            ["launchctl", "bootout", domain, str(path)],
            ["launchctl", "bootstrap", domain, str(path)],
        ]
        if installing
        else [
            ["launchctl", "print", f"{domain}/{LABEL}"],
            ["launchctl", "bootout", domain, str(path)],
        ]
    )
    if dry_run:
        print(f"{'write' if installing else 'remove'} {path}")
        for command in commands:
            print(shlex.join(command))
        return 0
    if installing:
        write_private(path, files[path])
        private_dir(home / "Library/Logs/upstream-watch")
    for command in commands:
        result = subprocess.run(command, capture_output=True, check=False)  # noqa: S603
        if not installing and "print" in command and result.returncode:
            break
        if result.returncode and (not installing or "bootout" not in command):
            raise WatchError(f"Scheduler command failed: {command[0]} {command[1]}")
    if not installing:
        path.unlink(missing_ok=True)
    return 0
