import json
from pathlib import Path

import pytest

from upstream_watch import state
from upstream_watch.config import (
    Config,
    WatchError,
    config_path,
    dump,
    expand,
    load,
    state_path,
    write_private,
)


def test_config_roundtrip_and_modes(tmp_path):
    path = tmp_path / "private/config.toml"
    config = Config(own=["me-example"], roots=[str(tmp_path)], repos=['path with "quotes"'])
    write_private(path, dump(config), exclusive=True)
    assert load(path) == config
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        write_private(path, dump(Config()), exclusive=True)
    assert load(path) == config
    write_private(path, dump(Config()))
    assert load(path) == Config()


@pytest.mark.parametrize(
    "content",
    [
        "not toml",
        "unknown = 3",
        "max_depth = true",
        "max_depth = -1",
        "max_depth = 101",
        "own = [4]",
        'own = [""]',
        'auto_update_blocked_by = ["urgent"]',
        'github_api = "yes"',
    ],
)
def test_invalid_config(tmp_path, content):
    path = tmp_path / "config.toml"
    path.write_text(content)
    with pytest.raises(WatchError):
        load(path)


def test_state_atomic_and_failed_replace(tmp_path, monkeypatch):
    path = tmp_path / "state/state.json"
    data = state.load(path)
    assert data == {"repos": {}, "api": {}}
    state.save(path, data)
    assert state.load(path) == data
    assert path.stat().st_mode & 0o777 == 0o600

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr("upstream_watch.config.os.replace", fail)
    with pytest.raises(OSError):
        state.save(path, {"repos": {"changed": {}}, "api": {}})
    assert state.load(path) == data
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("value", ["bad", "[]", '{"repos": []}', "{}"])
def test_invalid_state(tmp_path, value):
    path = tmp_path / "state.json"
    path.write_text(value)
    with pytest.raises(WatchError):
        state.load(path)


def test_paths_and_symlink_rejection(tmp_path, monkeypatch):
    assert str(config_path()).endswith("/.config/upstream-watch/config.toml")
    assert str(state_path()).endswith("/.local/state/upstream-watch/state.json")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.delenv("XDG_STATE_HOME")
    assert config_path().is_relative_to(Path.home())
    assert state_path().is_relative_to(Path.home())
    for value in ("~someone/src", "bad\0path"):
        with pytest.raises(WatchError):
            expand(value)
    link = tmp_path / "link"
    target = tmp_path / "target"
    target.write_text("keep")
    link.symlink_to(target)
    with pytest.raises(WatchError):
        write_private(link, b"replace")
    assert target.read_text() == "keep"
    directory = tmp_path / "alias"
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WatchError):
        write_private(directory / "new", b"data")


def test_dump_strings_remain_toml(tmp_path):
    config = Config(own=["me-example"], roots=["~/a\\b", "~/one\ntwo", "~/\U0001f431"])
    path = tmp_path / "config.toml"
    path.write_bytes(dump(config))
    assert load(path) == config
    assert json.loads('"hello"') == "hello"
