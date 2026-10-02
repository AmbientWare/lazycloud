from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def sanitize_python_packages(packages: Iterable[str]) -> list[str]:
    sanitized: list[str] = []
    for raw in packages:
        line = _strip_requirement_comment(raw.strip())
        if not line:
            continue
        if line.startswith("-"):
            sanitized.append(line)
        else:
            sanitized.append("".join(line.split()))
    return sanitized


def load_requirements_file(path: str | Path) -> list[str]:
    requirements_path = Path(path)
    return sanitize_python_packages(requirements_path.read_text(encoding="utf-8").splitlines())


def _strip_requirement_comment(value: str) -> str:
    for index, character in enumerate(value):
        if character == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].strip()
    return value.strip()
