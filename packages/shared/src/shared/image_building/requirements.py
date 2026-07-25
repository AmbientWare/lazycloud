from __future__ import annotations

import re
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


def requirement_package_name(requirement: str) -> str:
    line = _strip_requirement_comment(requirement.strip())
    if not line:
        return ""
    if line.startswith(("-i ", "--index-url ", "--extra-index-url ", "--find-links ")):
        return ""
    if line.startswith(("git+", "-e git+")):
        match = re.search(r"[#&]egg=([^&\s]+)", line)
        return _normalize_requirement_name(match.group(1)) if match else ""
    match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", line)
    if match is None:
        return ""
    return _normalize_requirement_name(match.group(1))


def _strip_requirement_comment(value: str) -> str:
    for index, character in enumerate(value):
        if character == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].strip()
    return value.strip()


def _normalize_requirement_name(value: str) -> str:
    return value.strip().replace("_", "-").lower()
