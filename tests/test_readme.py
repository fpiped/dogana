import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PAGES = {
    name: re.findall(r"^```([^\n]*)\n(.*?)^```", (ROOT / name).read_text(), re.MULTILINE | re.DOTALL)
    for name in ("README.md", "docs/quickstart.md")
}


@pytest.mark.parametrize("page", sorted(PAGES))
def test_every_documented_block_runs_from_its_documented_directory(tmp_path, page):
    pytest.importorskip("duckdb")
    BLOCKS = PAGES[page]
    assert BLOCKS and all(language in ("sh", "python", "text", "json") for language, _ in BLOCKS)
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
        elif language == "python":
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
    assert result.stdout == "".join(code for language, code in BLOCKS if language == "text")
