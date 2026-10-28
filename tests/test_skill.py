import re
import subprocess
import sys
from pathlib import Path

import dogana

SKILL = Path(dogana.__file__).parent / ".agents" / "skills" / "dogana" / "SKILL.md"


def test_the_skill_names_itself_after_its_folder_and_says_when_to_use_it():
    front, body = SKILL.read_text(encoding="utf-8").removeprefix("---\n").split("\n---\n", 1)
    fields = dict(line.split(": ", 1) for line in front.splitlines())
    assert fields["name"] == SKILL.parent.name
    assert 0 < len(fields["description"]) <= 1024
    assert body.lstrip().startswith("# dogana")


def test_every_python_block_of_the_skill_runs(tmp_path):
    blocks = re.findall(r"^```python\n(.*?)^```", SKILL.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
    assert blocks
    (tmp_path / "orders-changes.csv").write_bytes(b"id,order_date,updated_at,deleted\n")
    subprocess.run([sys.executable, "-c", "\n".join(blocks)], cwd=tmp_path, check=True, capture_output=True)
