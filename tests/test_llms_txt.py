import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_llms_txt_links_every_page_of_the_site_as_markdown(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "scripts" / "llms_txt.py"), str(tmp_path)], check=True)
    index = (tmp_path / "llms.txt").read_text(encoding="utf-8")
    pages = re.findall(r"^  - [^:\n]+: (\S+\.md)$", (ROOT / "mkdocs.yml").read_text(), re.MULTILINE)
    links = re.findall(r"\]\(https://fpiped\.github\.io/dogana/(\S+\.md)\)", index)
    assert index.startswith("# dogana\n\n> ") and len(links) == len(pages)
    full = (tmp_path / "llms-full.txt").read_text(encoding="utf-8")
    for link in links:
        text = (tmp_path / link).read_text(encoding="utf-8")
        assert text.strip() and text.strip() in full
    assert "--8<--" not in full
