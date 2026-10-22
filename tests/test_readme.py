import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BLOCKS = re.findall(r"^```([^\n]*)\n(.*?)^```", (ROOT / "README.md").read_text(), re.MULTILINE | re.DOTALL)


def test_readme_installation_command_runs_from_the_checkout():
    commands = [code for language, code in BLOCKS if language == "sh"]
    assert commands
    env = dict(
        os.environ,
        UV_CACHE_DIR=str(ROOT / ".venv" / "uv-cache"),
        UV_TOOL_DIR=str(ROOT / ".venv" / "uv-tools"),
    )
    for command in commands:
        subprocess.run(shlex.split(command), cwd=ROOT, env=env, check=True, capture_output=True, text=True)


def test_every_readme_python_block_runs_in_order_and_verifies_its_claims(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    assert BLOCKS and all(language in ("sh", "python") for language, _ in BLOCKS)
    monkeypatch.chdir(tmp_path)
    namespace = {"__name__": "__main__"}
    for index, (language, code) in enumerate(BLOCKS):
        if language == "python":
            exec(compile(code, f"README.md:block{index}", "exec"), namespace)
    assert len(namespace["history"]) == 3
    assert namespace["request"].shape == "daily"
