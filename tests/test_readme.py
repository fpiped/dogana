import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BLOCKS = re.findall(r"^```([^\n]*)\n(.*?)^```", (ROOT / "README.md").read_text(), re.MULTILINE | re.DOTALL)


def test_every_readme_block_runs_from_its_documented_directory(tmp_path):
    pytest.importorskip("duckdb")
    assert BLOCKS and all(language in ("sh", "python") for language, _ in BLOCKS)
    environment = dict(
        os.environ,
        VIRTUAL_ENV=str(Path(sys.executable).parent.parent),
        UV_CACHE_DIR=str(tmp_path / "uv-cache"),
    )
    scripts = []
    for language, code in BLOCKS:
        if language == "sh":
            subprocess.run(
                shlex.split(code), cwd=ROOT, env=environment, check=True, capture_output=True, text=True
            )
        else:
            scripts.append(code)
    example = tmp_path / "empty directory"
    example.mkdir()
    assert not tuple(example.iterdir())
    result = subprocess.run(
        [sys.executable, "-c", "\n".join(scripts)],
        cwd=example,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.count("Success:") == len(scripts)
