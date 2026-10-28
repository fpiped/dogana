"""Write llms.txt and llms-full.txt into a built site, and each page's Markdown at its path under docs."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SITE_URL = "https://fpiped.github.io/dogana/"


def nav() -> list[tuple[str, str]]:
    """The site's pages in navigation order, as (title, path under docs)."""
    text = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    return re.findall(r"^  - ([^:\n]+): (\S+\.md)$", text.split("\nnav:\n", 1)[1], re.MULTILINE)


def page(path: str) -> str:
    """A page's Markdown, with the home page's README include resolved."""
    if path == "index.md":
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        return readme.split("[start:body] -->\n", 1)[1].split("<!-- --8<-- [end:body]", 1)[0]
    return (ROOT / "docs" / path).read_text(encoding="utf-8")


def main(site: Path) -> None:
    description = re.search(r'^site_description: "(.+)"$', (ROOT / "mkdocs.yml").read_text(), re.MULTILINE)
    assert description is not None
    pages = nav()
    links = []
    for title, path in pages:
        (site / path).parent.mkdir(parents=True, exist_ok=True)
        (site / path).write_text(page(path), encoding="utf-8")
        links.append(f"- [{title}]({SITE_URL}{path})")
    (site / "llms.txt").write_text(
        f"# dogana\n\n> {description.group(1)}.\n\n"
        "The protocol is the specification; the other pages explain and apply it.\n\n"
        "## Docs\n\n" + "\n".join(links) + "\n",
        encoding="utf-8",
    )
    (site / "llms-full.txt").write_text(
        "\n\n".join(page(path).strip() for _, path in pages) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
