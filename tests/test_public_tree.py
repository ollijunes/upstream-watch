import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "privacy_checker", Path(__file__).parents[1] / "scripts/check_public_tree.py"
)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_canaries_and_allowed_examples():
    checker.self_test()
    assert not checker.detect(b"Synthetic <synthetic@example.com>")
    assert not checker.detect(b"GitHub <noreply@github.com>")
    assert checker.private_path("config.toml")
    assert not checker.private_path("examples/config.toml")
    assert not checker.private_path("examples/home/.codex/config.toml")


@pytest.mark.parametrize(
    "value",
    [
        "@".join(("someone", "unapproved" + ".com")),
        "/".join(("", "Users", "synthetic", "note")),
        "+" + "358" + "401234567",
        ".".join(("192", "168", "1", "1")),
    ],
)
def test_private_values(value):
    assert checker.detect(value.encode())


def test_invalid_date_personal_code():
    number = "32" + "13" + "00" + "123"
    check = chr(checker.CHECK_CHARACTERS[int(number) % 31])
    value = number[:6] + "A" + number[6:] + check
    assert not checker.detect(value.encode())


def test_relative_synthetic_home_is_not_a_personal_absolute_path():
    assert not checker.detect(b"examples/home/.claude/CLAUDE.md")
    assert not checker.detect(b"examples/home/.codex/config.toml")
    absolute = "/".join(("", "home", "synthetic", "note"))
    for prefix in ("", '"', "path=", "\n"):
        assert "personal home directory" in checker.detect((prefix + absolute).encode())


@pytest.mark.parametrize(
    "path",
    [
        ".claude/CLAUDE.md",
        ".codex/AGENTS.md",
        "auth.json",
        ".credentials.json",
        ".claude.json",
        "transcript.jsonl",
    ],
)
def test_agent_runtime_paths_rejected_outside_exact_fixtures(path):
    assert checker.private_path(path)
    assert checker.private_path("examples/home/.claude/new-private-note.md")
    assert not checker.private_path("examples/home/.claude/.credentials.json")
    assert not checker.private_path("examples/home/.codex/auth.json")
