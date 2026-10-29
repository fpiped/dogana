import io
import json
import os
import re
import subprocess
import sys
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]


@dataclass
class Block:
    language: str
    code: str
    line: int
    illustration: str | None = None


def blocks(markdown):
    lines = markdown.splitlines(keepends=True)
    result = []
    index = 0
    while index < len(lines):
        opening = re.fullmatch(r" {0,3}(`{3,}|~{3,})([^\n]*)\n?", lines[index])
        if opening is None:
            index += 1
            continue
        fence, info = opening.groups()
        line = index + 1
        previous = "".join(lines[:index]).rstrip()
        illustration = re.search(r"<!-- illustration: (.*?) -->$", previous)
        assert "<!-- illustration" not in previous.split("\n")[-1] or illustration, (
            f"line {line}: use <!-- illustration: reason -->"
        )
        reason = illustration[1].strip() if illustration else None
        assert reason != "", f"line {line}: an illustration needs a reason"
        language = info.split()[0] if info.strip() else ""
        index += 1
        start = index
        closing = rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*\n?"
        while index < len(lines) and not re.fullmatch(closing, lines[index]):
            index += 1
        assert index < len(lines), f"line {line}: unclosed fence"
        result.append(Block(language, "".join(lines[start:index]), line, reason))
        index += 1
    return result


def run_blocks(page, examples):
    namespace = {"__name__": "__main__", "documentation": ROOT}
    for index, block in enumerate(examples):
        location = f"{page}:{block.line}"
        if block.illustration:
            continue
        if block.language == "text":
            assert index and examples[index - 1].language in ("python", "sh"), (
                f"{location}: text needs a preceding executable block or an illustration reason"
            )
            assert not examples[index - 1].illustration, f"{location}: expected output of an illustration"
            continue
        expected = ""
        if index + 1 < len(examples):
            following = examples[index + 1]
            if following.language == "text" and not following.illustration:
                expected = following.code
        output = io.StringIO()
        with redirect_stdout(output):
            if block.language == "python":
                # Keep tracebacks aligned with the Markdown source.
                exec(compile("\n" * block.line + block.code, page, "exec"), namespace)
            elif block.language == "sql":
                query = block.code
                for expression, value in namespace.get("sql_bindings", {}).items():
                    query = query.replace(expression, value)
                assert "{{" not in query, f"{location}: unresolved SQL template"
                namespace["sql_query"] = query
                namespace["sql_result"] = namespace["connection"].execute(query).fetchall()
            elif block.language == "sh":
                result = subprocess.run(
                    ["sh", "-eu", "-c", block.code], check=True, capture_output=True, text=True
                )
                print(result.stdout, end="")
            else:
                raise AssertionError(f"{location}: unsupported fence {block.language!r}; mark illustrations")
        assert output.getvalue() == expected, (
            f"{location}: stdout differs\nexpected: {expected!r}\nactual:   {output.getvalue()!r}"
        )


def run_page(tmp_path, page, examples):
    assert not tuple(tmp_path.iterdir())
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), page, json.dumps([asdict(b) for b in examples])],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH="", LC_ALL="C", TZ="UTC"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"{page}\n{result.stdout}{result.stderr}"
    assert result.stdout == "", result.stdout


@pytest.mark.parametrize("page", PAGES, ids=lambda page: str(page.relative_to(ROOT)))
def test_every_documented_block_runs_from_an_empty_directory(tmp_path, page):
    run_page(tmp_path, str(page), blocks(page.read_text(encoding="utf-8")))


def test_fence_attributes_and_hidden_setup_keep_running_while_illustrations_need_a_reason(tmp_path):
    examples = blocks(
        "<!-- test setup: shared value\n\n```python\nx = 3\n```\n\n-->\n"
        '```python title="example"\nprint(x)\n```\n```text\n3\n```\n'
        "<!-- illustration: a loader supplies this function -->\n```python\nloader()\n```\n"
        "~~~python\nassert x == 3\n~~~\n"
    )
    assert len(examples) == 5 and examples[3].illustration
    run_page(tmp_path, "example.md", examples)


@pytest.mark.parametrize(
    ("markdown", "message"),
    [
        ("```yaml\nversion: 2\n```\n", "unsupported fence"),
        ("```text\norphan\n```\n", "text needs a preceding"),
        ("```python\nprint('unexpected')\n```\n", "stdout differs"),
        (
            "```python\nprint('first')\n```\n```text\nsecond\n```\n"
            "```python\nprint('second')\n```\n```text\nfirst\n```\n",
            "stdout differs",
        ),
        ("<!-- illustration: fragment -->\n```python\nx()\n```\n```text\nx\n```\n", "illustration"),
    ],
)
def test_unchecked_blocks_and_wrong_output_fail(markdown, message):
    with pytest.raises(AssertionError, match=message):
        run_blocks("example.md", blocks(markdown))


@pytest.mark.parametrize("markdown", ["```python\nx = 1\n", "<!-- illustration:  -->\n```text\nx\n```\n"])
def test_incomplete_fences_and_empty_illustration_reasons_fail(markdown):
    with pytest.raises(AssertionError):
        blocks(markdown)


if __name__ == "__main__":
    run_blocks(sys.argv[1], [Block(**value) for value in json.loads(sys.argv[2])])
