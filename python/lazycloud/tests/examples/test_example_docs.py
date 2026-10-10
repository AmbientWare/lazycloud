from __future__ import annotations

import re
from pathlib import Path

import pytest
from lazycloud.example_catalog import example_catalog

DOCS = Path(__file__).resolve().parents[4] / "docs" / "examples"
DOWNLOAD = re.compile(r"lazycloud example download ([a-z][a-z0-9-]*)")
FENCE = re.compile(r"^```[\w-]+ ([^\s`]+)[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)


@pytest.mark.parametrize("page", sorted(DOCS.glob("*.mdx")), ids=lambda page: page.stem)
def test_titled_code_blocks_match_the_shipped_project(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    quotes = FENCE.findall(text)
    if not quotes:
        return
    names = set(DOWNLOAD.findall(text)) - {"all"}
    assert len(names) == 1, f"{page.name} downloads more than one example: {sorted(names)}"
    project = example_catalog()[names.pop()]
    for path, body in quotes:
        assert path in project.content, f"{page.name} quotes {path}, which the project lacks"
        assert body in project.content[path].decode("utf-8"), (
            f"{page.name} quotes {path} with code that differs from the project"
        )
