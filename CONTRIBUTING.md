# Contributing

Read AGENTS.md before working in the repository.
Work in a dedicated Git worktree, not the shared checkout.
Change docs/protocol.md first when changing semantics, and state the reason.
Keep the core on Python 3.11+ and the standard library only.
Install development dependencies with uv pip install -e '.[dev,duckdb]'.
Run .venv/bin/python -m pytest -q for the affected behavior, then the required suite.
Run uvx ruff check ., uvx ruff format --check . and .venv/bin/python -m mypy --strict src.
There is no release process yet.
