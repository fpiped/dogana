# Contributing

Read AGENTS.md before working in the repository.
Work in a dedicated Git worktree, not the shared checkout.
Change docs/protocol.md first when changing semantics, and state the reason.
Keep the core on Python 3.11+ and the standard library only.
Install development dependencies with uv pip install -e . --group dev.
Run .venv/bin/python -m pytest -q for the affected behavior, then the whole suite with the same command.
Claim rules with `@pytest.mark.protocol("transitions.3", "manifest.4")`; run
`.venv/bin/python scripts/traceability.py --list` for IDs and coverage (see [Traceability](docs/traceability.md)).
A coverage floor prevents regressions; run .venv/bin/coverage run -m pytest -q && .venv/bin/coverage report,
with COVERAGE_CORE=sysmon on Python 3.14, where tracing costs nothing.
Run uvx ruff check ., uvx ruff format --check . and .venv/bin/python -m mypy --strict src scripts/traceability.py.
The package never reads a row or a file; tests/loader.py is the receiver the tests use, in memory.
The pytest fixture validate_wire(value) validates raw decoded JSON against docs/wire. A session fixture
also validates every public Wire.to_dict() result, through to_json() too; subprocesses, collection-time
calls, direct _encode() or _document() calls and identity forms bypass it.
A release is a GitHub release whose tag, vX.Y.Z, names the version in pyproject.toml; the release
workflow builds the package and publishes it to PyPI.
A change to the public API or to a rule updates src/dogana/.agents/skills/dogana/SKILL.md too.

Documentation examples are tested by tests/test_docs.py, which discovers README.md and every
Markdown page under docs/. Each page runs in one fresh Python process from an empty directory,
with dogana installed. Python blocks share variables in source order;
fence attributes such as `python title="example"` do not disable execution. A following `text`
block is the preceding Python or shell block's exact stdout, including trailing newlines. A block
without expected output must print nothing. Unpaired text and unsupported languages fail.
Shell blocks run with `sh -eu` in the same directory; do not reinstall dependencies in examples.

Mark a non-executable fragment with `<!-- illustration: reason -->` immediately before its fence.
Use a concrete reason, such as loader pseudocode or a CSV sample. The comment stays in the source
and is invisible on the site. No fence attribute disables tests. When setup or assertions would
clutter the page, put their Python fences inside a multiline HTML comment beginning
`<!-- test setup: reason` and ending `-->`. Those blocks run normally but remain invisible.
Prefer visible imports and setup that a reader needs to copy an example.

SQL blocks run on the `connection` created by preceding Python and leave `sql_query` and
`sql_result` for assertions in the next Python block. Use hidden assertions to check results and
failure cases. The optional `sql_bindings` dictionary replaces exact dbt template expressions
with fixture SQL or values; unresolved templates fail, and no dbt adapter is required.
Hidden setup can read shared documentation fixtures relative to `documentation`, the repository
root, as Integrations does for Reference's view. Keep all generated files in the empty directory.
Run `uvx zensical==0.0.73 build --strict` after changing fences or hidden comments.
