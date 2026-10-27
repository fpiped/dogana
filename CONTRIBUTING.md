# Contributing

Read AGENTS.md before working in the repository.
Work in a dedicated Git worktree, not the shared checkout.
Change docs/protocol.md first when changing semantics, and state the reason.
Keep the core on Python 3.11+ and the standard library only.
Install development dependencies with uv pip install -e '.[dev,duckdb]'.
Run .venv/bin/python -m pytest -q for the affected behavior, then the whole suite with the same command.
Run uvx ruff check ., uvx ruff format --check . and .venv/bin/python -m mypy --strict src.
The pytest fixture validate_wire(value) validates raw decoded JSON against docs/wire. A session fixture
also validates every public Wire.to_dict() result, through to_json() too; subprocesses, collection-time
calls, direct encode() or _document() calls and identity forms bypass it.
A release is a GitHub release whose tag, vX.Y.Z, names the version in pyproject.toml; the release
workflow builds the package and publishes it to PyPI.
