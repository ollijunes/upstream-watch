import pytest

from upstream_watch.config import Config
from upstream_watch.discover import discover, parse_remote
from upstream_watch.github import GitHub

from .conftest import git


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/widget.git",
        "git@" + "github.com:acme/widget.git",
        "ssh://git@" + "github.com/acme/widget",
        "https://github.com/acme/widget/",
    ],
)
def test_remote_formats(url):
    assert parse_remote(url) == "acme/widget"


@pytest.mark.parametrize(
    "url",
    [
        "git@alias:acme/widget",
        "https://example.org/acme/widget",
        "https://github.com.evil.example.com/acme/widget",
        "https://credential@" + "github.com/acme/widget",
        "https://github.com/acme/widget?token=x",
    ],
)
def test_remote_rejected(url):
    assert parse_remote(url) is None


def test_own_and_upstream(clone):
    config = clone.config(own=["ME-EXAMPLE"])
    git(clone.path, "remote", "set-url", "origin", "https://github.com/me-example/widget")
    assert discover(config)[0].reason == "own"
    git(clone.path, "remote", "add", "upstream", "https://github.com/acme/widget")
    assert discover(config)[0].remote == "upstream"
    git(clone.path, "remote", "add", "other", "https://github.com/example-org/widget")
    assert discover(config)[0].remote == "upstream"


def test_no_remote_and_non_github(clone):
    git(clone.path, "remote", "set-url", "origin", "git@alias:acme/widget")
    assert discover(clone.config())[0].reason == "not-github"
    git(clone.path, "remote", "remove", "origin")
    assert discover(clone.config())[0].reason == "no-remote"


def test_walk_depth_symlinks_and_exclude(factory, tmp_path):
    root = tmp_path / "roots"
    top = factory(parent=root)
    deep = factory("acme/deep", parent=root / "nested")
    hidden = factory("acme/hidden", parent=root / ".hidden")
    vendor = factory("acme/vendor", parent=root / "node_modules")
    (root / "linked").symlink_to(deep.path, target_is_directory=True)
    config = Config(roots=[str(root)], max_depth=1, github_api=False)
    assert [r.path for r in discover(config)] == [top.path]
    config.max_depth = 2
    assert {r.path for r in discover(config)} == {top.path, deep.path}
    config.exclude = [str(root / "nested/*")]
    assert next(r for r in discover(config) if r.path == deep.path).reason == "excluded"
    assert hidden.path not in [r.path for r in discover(config)]
    assert vendor.path not in [r.path for r in discover(config)]
    config.roots = [str(root / "linked")]
    assert discover(config)[0].reason == "symlink"


def test_from_extra_and_bare(clone, tmp_path):
    source = tmp_path / "paths.txt"
    source.write_text(f"# examples\n{clone.path}\n{clone.upstream} # bare\n")
    result = discover(Config(roots=["/nonexistent"], repos=["/also-nonexistent"]), source=source)
    assert result[0].full_name == "acme/widget"
    assert result[1].reason == "not-a-repo-root"
    assert discover(Config(roots=[str(clone.upstream)]))[0].reason == "not-a-repo-root"
    assert (
        discover(Config(roots=[], repos=[str(tmp_path / "missing")]))[0].reason == "not-a-repo-root"
    )


def test_worktree_and_submodule(clone, factory, tmp_path):
    worktree = tmp_path / "worktree"
    git(clone.path, "worktree", "add", "-b", "work", str(worktree))
    config = clone.config()
    config.repos.append(str(worktree))
    assert [r.reason for r in discover(config)] == ["", "worktree-duplicate"]
    other = factory("acme/child")
    git(
        clone.path,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(other.upstream),
        "child",
    )
    config.repos = [str(clone.path / "child")]
    assert discover(config)[0].reason == "worktree-duplicate"
    config.roots = [str(clone.path)]
    config.repos = []
    assert len(discover(config)) == 1


def test_public_check_and_disabled(clone, fake_api):
    routes, requests, base = fake_api
    api = GitHub({}, base=base)
    config = clone.config()
    config.github_api = True
    assert discover(config, api)[0].reason == "not-public"
    routes["/repos/acme/widget"] = (
        200,
        {"full_name": "acme/widget", "owner": {"login": "acme"}, "private": False},
        {},
    )
    assert discover(config, api)[0].metadata["owner"]["login"] == "acme"
    config.github_api = False
    requests.clear()
    assert discover(config, api)[0].reason == ""
    assert not requests
    routes["/repos/acme/widget"] = (200, {"private": True}, {})
    config.github_api = True
    api = GitHub({}, base=base)
    assert discover(config, api)[0].reason == "not-public"


def test_get_url_uses_git_rewrite(clone):
    git(clone.path, "remote", "set-url", "origin", "https://example.org/acme/widget")
    git(clone.path, "config", "url.https://github.com/.insteadOf", "https://example.org/")
    assert discover(clone.config())[0].full_name == "acme/widget"


def test_explicit_symlink_excluded(clone, tmp_path):
    link = tmp_path / "alias"
    link.symlink_to(clone.path, target_is_directory=True)
    config = clone.config()
    config.repos = [str(link)]
    assert discover(config)[0].reason == "symlink"
