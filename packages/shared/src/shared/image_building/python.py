from __future__ import annotations

import re

from shared.image_building.authoring import PythonVersion


def normalize_python_version(value: str) -> str:
    value = value.strip().removeprefix("python")
    prefix = "micromamba" if value.startswith("micromamba") else ""
    release = value.removeprefix(prefix) if prefix else value
    if not re.fullmatch(r"3\.\d+(?:\.\d+)?", release):
        raise ValueError(f"Python version must be major.minor or major.minor.patch: {value!r}")
    minor = ".".join(release.split(".")[:2])
    if minor not in {version.value for version in PythonVersion}:
        raise ValueError(f"unsupported Python version: {value!r}")
    return prefix + release


def python_minor_version(value: str) -> str:
    release = normalize_python_version(value).removeprefix("micromamba")
    return ".".join(release.split(".")[:2])
