import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from upstream_watch import github
from upstream_watch.config import Config
from upstream_watch.discover import Repo


def git(path, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=check
    ).stdout.strip()


@pytest.fixture(scope="session", autouse=True)
def loopback_only():
    original_connect = socket.socket.connect
    original_create = socket.create_connection

    def allowed(address):
        host = address[0]
        if host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise OSError(f"Test blocked non-loopback connection: {host}")

    def connect(sock, address):
        allowed(address)
        return original_connect(sock, address)

    def create(address, *args, **kwargs):
        allowed(address)
        return original_create(address, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", connect)
        patch.setattr(socket, "create_connection", create)
        yield


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setattr(
        os,
        "environ",
        {
            "PATH": os.defpath + os.pathsep + str(Path(sys.executable).parent),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_STATE_HOME": str(home / ".local/state"),
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Example Author",
            "GIT_COMMITTER_NAME": "Example Author",
            "GIT_AUTHOR_EMAIL": "author@example.com",
            "GIT_COMMITTER_EMAIL": "author@example.com",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "file",
        },
    )
    monkeypatch.setattr(github, "runtime_token", lambda: None)
    monkeypatch.setattr("upstream_watch.cli.runtime_token", lambda: None)
    original = subprocess.run
    store = tmp_path / "remotes"
    store.mkdir()

    def transport(argv, *args, **kwargs):
        kwargs.setdefault("env", dict(os.environ))
        if (
            isinstance(argv, (list, tuple))
            and argv[0] == "git"
            and any(cmd in argv for cmd in ("fetch", "pull", "ls-remote"))
        ):
            argv = [
                argv[0],
                "-c",
                f"url.{store.as_uri()}/.insteadOf=https://github.com/",
                *argv[1:],
            ]
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", transport)
    return home, store


@dataclass
class Clone:
    upstream: Path
    source: Path
    path: Path
    repo: Repo

    def commit(self, name="file.txt", content="change\n", subject="feat: change", email=None):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        git(self.source, "add", name)
        if email:
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("GIT_AUTHOR_EMAIL", email)
                patch.setenv("GIT_COMMITTER_EMAIL", email)
                git(self.source, "commit", "-m", subject)
        else:
            git(self.source, "commit", "-m", subject)
        git(self.source, "push", "origin", "HEAD:main")
        return git(self.source, "rev-parse", "HEAD")

    def tag(self, name, *, annotated=False):
        git(self.source, "tag", *(["-a", "-m", name] if annotated else []), name)
        git(self.source, "push", "origin", "--tags")

    def config(self, **kwargs):
        return Config(roots=[], repos=[str(self.path)], github_api=False, **kwargs)


@pytest.fixture
def factory(tmp_path, isolated):
    _, store = isolated
    tmp_path = tmp_path.resolve()
    counter = 0

    def make(full_name="acme/widget", *, parent=None, shallow=False):
        nonlocal counter
        counter += 1
        bare = store / (full_name + ".git")
        bare.parent.mkdir(parents=True, exist_ok=True)
        git(tmp_path, "init", "--bare", "--initial-branch=main", str(bare))
        source = tmp_path / f"source-{counter}"
        git(tmp_path, "clone", str(bare), str(source))
        (source / "file.txt").write_text("initial\n")
        git(source, "add", ".")
        git(source, "commit", "-m", "initial")
        git(source, "push", "origin", "main")
        target = (parent.resolve() if parent else tmp_path) / f"clone-{counter}"
        target.parent.mkdir(parents=True, exist_ok=True)
        git(tmp_path, "clone", *(["--depth=1"] if shallow else []), bare.as_uri(), str(target))
        git(target, "remote", "set-url", "origin", f"https://github.com/{full_name}.git")
        repo = Repo(target, "origin", full_name, str((target / ".git").resolve()))
        return Clone(bare, source, target, repo)

    return make


@pytest.fixture
def clone(factory):
    return factory()


@pytest.fixture
def saved():
    return {"repos": {}, "api": {}}


@pytest.fixture
def fake_api():
    routes = {}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            status, data, headers = routes.get(self.path, (404, {}, {}))
            if headers.get("ETag") and self.headers.get("If-None-Match") == headers["ETag"]:
                status = 304
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((str(ipaddress.IPv4Address(2130706433)), 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{server.server_address[0]}:{server.server_address[1]}"
    yield routes, requests, base
    server.shutdown()
    server.server_close()
    thread.join()
