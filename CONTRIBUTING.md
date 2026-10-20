# Contributing

Bug fixes, new risk signals with a clear rationale, and support for more
GitHub remote shapes are welcome. Support for other hosts is a larger
change; open an issue first.

```sh
uv sync --locked
uv run python -m pytest --cov=upstream_watch --cov-fail-under=90
uv run ruff check .
uv run ruff format --check .
gitleaks git --redact --no-banner .
python3 scripts/check_public_tree.py
```

## Rules

- Tests build their own upstream repositories and clones under `tmp_path`
  and use the fake GitHub API in `tests/`. No test may reach the network or
  read a clone outside the temporary directory.
- Never commit a report, a state file, a config or a path from your own
  machine. Fixture owners are `acme`, `example-org` and `me-example`.
- Build secret-shaped test values from fragments at run time, so this
  repository's own scans stay clean.
- Keep the runtime on the Python standard library and `git`.
- A new flag needs three things in `docs/flags.md`: how it is detected, why
  it matters and what the reader should do. Prefer `review` over `high`
  unless the signal is history rewriting or a change of ownership.
- Do not add any command that changes a clone other than `git pull
  --ff-only`, and do not weaken the refusal conditions in `update` to make
  a case work. Explain the case in an issue instead.
- `examples/report.md` is generated from the test fixtures and checked by a
  test. Regenerate it when the renderer changes, and read the result.

Use a public-safe Git author address. Report vulnerabilities through
[SECURITY.md](SECURITY.md), not a public issue.
