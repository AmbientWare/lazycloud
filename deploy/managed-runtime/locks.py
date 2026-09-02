from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK_ROOT = ROOT / "deploy" / "managed-runtime" / "locks"
MANAGED_PACKAGE_PATHS = (
    ROOT / "packages" / "shared",
    ROOT / "packages" / "foundation",
    ROOT / "packages" / "lazycloud",
    ROOT / "packages" / "runner",
)
MANAGED_DISTRIBUTIONS = frozenset({"foundation", "runner", "lazycloud-client", "lazycloud-shared"})
PYTHON_VERSIONS = ("3.10", "3.11", "3.12", "3.13", "3.14")
ARCHITECTURES = ("amd64", "arm64")
UV_PLATFORMS = {
    "amd64": "x86_64-manylinux_2_28",
    "arm64": "aarch64-manylinux_2_28",
}
LOCK_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^ ;\\]+)")
METADATA_DIGEST_LINE = re.compile(r"^# managed-metadata-sha256: ([a-f0-9]{64})$")
LOCK_SCHEMA_VERSION = 1
LOCK_TOOL_REQUIREMENTS = ("packaging>=24,<27",)
BUILD_TOOL_NAMES = frozenset({"packaging", "setuptools", "wheel"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "check"))
    parser.add_argument("--python-version", choices=PYTHON_VERSIONS, action="append")
    parser.add_argument("--architecture", choices=ARCHITECTURES, action="append")
    args = parser.parse_args()
    versions = tuple(args.python_version or PYTHON_VERSIONS)
    architectures = tuple(args.architecture or ARCHITECTURES)
    if args.command == "generate":
        generate_build_tools_lock()
    check_build_tools_lock()
    for version in versions:
        for architecture in architectures:
            if args.command == "generate":
                generate_lock(version, architecture)
            check_lock(version, architecture)


def generate_lock(python_version: str, architecture: str) -> None:
    LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    requirements = _target_requirements(python_version, architecture)
    command = [
        "uv",
        "pip",
        "compile",
        "-",
        "--python-version",
        python_version,
        "--python-platform",
        UV_PLATFORMS[architecture],
        "--only-binary",
        ":all:",
        "--generate-hashes",
        "--no-annotate",
        "--custom-compile-command",
        "python deploy/managed-runtime/locks.py generate",
        "--output-file",
        str(lock_path(python_version, architecture)),
    ]
    subprocess.run(
        command,
        cwd=ROOT,
        input="\n".join(requirements) + "\n",
        text=True,
        check=True,
    )
    _write_metadata_digest(lock_path(python_version, architecture))


def check_lock(python_version: str, architecture: str) -> None:
    path = lock_path(python_version, architecture)
    if not path.is_file():
        raise RuntimeError(f"managed runtime lock is missing: {path.relative_to(ROOT)}")
    content = path.read_text(encoding="utf-8")
    _check_metadata_digest(content, path)
    pins = _locked_versions(content, path)
    unexpected = sorted(MANAGED_DISTRIBUTIONS.intersection(pins))
    if unexpected:
        raise RuntimeError(
            f"{path.name} contains release-managed distributions: {', '.join(unexpected)}"
        )


def generate_build_tools_lock() -> None:
    LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    command = [
        "uv",
        "pip",
        "compile",
        "-",
        "--python-version",
        "3.10",
        "--python-platform",
        UV_PLATFORMS["amd64"],
        "--only-binary",
        ":all:",
        "--generate-hashes",
        "--no-annotate",
        "--custom-compile-command",
        "python deploy/managed-runtime/locks.py generate",
        "--output-file",
        str(build_tools_lock_path()),
    ]
    subprocess.run(
        command,
        cwd=ROOT,
        input="\n".join(_build_requirements()) + "\n",
        text=True,
        check=True,
    )
    _write_metadata_digest(build_tools_lock_path())


def check_build_tools_lock() -> None:
    path = build_tools_lock_path()
    if not path.is_file():
        raise RuntimeError(f"managed runtime build-tools lock is missing: {path.relative_to(ROOT)}")
    content = path.read_text(encoding="utf-8")
    _check_metadata_digest(content, path)
    pins = _locked_versions(content, path)
    missing = sorted(BUILD_TOOL_NAMES - pins.keys())
    if missing:
        raise RuntimeError(f"{path.name} is missing build tools: {', '.join(missing)}")


def lock_path(python_version: str, architecture: str) -> Path:
    version = python_version.replace(".", "")
    return LOCK_ROOT / f"python{version}-linux-{architecture}.txt"


def build_tools_lock_path() -> Path:
    return LOCK_ROOT / "build-tools.txt"


def _target_requirements(python_version: str, architecture: str) -> list[str]:
    import tomllib

    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    environment: dict[str, str] = {key: str(value) for key, value in default_environment().items()}
    environment.update(
        {
            "python_version": python_version,
            "python_full_version": f"{python_version}.0",
            "sys_platform": "linux",
            "platform_system": "Linux",
            "platform_machine": "x86_64" if architecture == "amd64" else "aarch64",
        }
    )
    requirements: dict[str, Requirement] = {}
    for package_path in MANAGED_PACKAGE_PATHS:
        project = tomllib.loads((package_path / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]
        for value in project["dependencies"]:
            requirement = Requirement(value)
            name = canonicalize_name(requirement.name)
            if name in MANAGED_DISTRIBUTIONS:
                continue
            if requirement.marker is not None and not requirement.marker.evaluate(environment):
                continue
            existing = requirements.get(name)
            if existing is not None and str(existing) != str(requirement):
                raise RuntimeError(
                    f"managed packages declare conflicting requirements for {name}: "
                    f"{existing} and {requirement}"
                )
            requirements[name] = requirement
    return [str(requirements[name]) for name in sorted(requirements)]


def _build_requirements() -> list[str]:
    import tomllib

    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    requirements = {
        canonicalize_name(requirement.name): requirement
        for value in LOCK_TOOL_REQUIREMENTS
        if (requirement := Requirement(value))
    }
    for package_path in MANAGED_PACKAGE_PATHS:
        pyproject = tomllib.loads((package_path / "pyproject.toml").read_text(encoding="utf-8"))
        for value in pyproject["build-system"]["requires"]:
            requirement = Requirement(value)
            name = canonicalize_name(requirement.name)
            existing = requirements.get(name)
            if existing is not None and str(existing) != str(requirement):
                raise RuntimeError(
                    f"managed packages declare conflicting build requirements for {name}: "
                    f"{existing} and {requirement}"
                )
            requirements[name] = requirement
    return [str(requirements[name]) for name in sorted(requirements)]


def _locked_versions(content: str, path: Path) -> dict[str, str]:
    lines = content.splitlines()
    pins: dict[str, str] = {}
    starts: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        match = LOCK_LINE.match(line)
        if match is not None:
            starts.append((index, _canonical_name(match.group(1)), match.group(2)))
    for position, (index, name, version) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        if not any("--hash=sha256:" in line for line in lines[index:end]):
            raise RuntimeError(f"{path.name} pin {name}=={version} is not hash-locked")
        if name in pins:
            raise RuntimeError(f"{path.name} contains multiple pins for {name}")
        pins[name] = version
    if not pins:
        raise RuntimeError(f"managed runtime lock has no package pins: {path}")
    return pins


def _managed_metadata_digest() -> str:
    digest = hashlib.sha256()
    digest.update(f"managed-runtime-lock-schema:{LOCK_SCHEMA_VERSION}".encode())
    digest.update(b"\0")
    for package_path in MANAGED_PACKAGE_PATHS:
        path = package_path / "pyproject.toml"
        digest.update(package_path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    digest.update(json.dumps(LOCK_TOOL_REQUIREMENTS).encode())
    return digest.hexdigest()


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _write_metadata_digest(path: Path) -> None:
    content = path.read_text(encoding="utf-8")
    without_old_digest = "\n".join(
        line for line in content.splitlines() if METADATA_DIGEST_LINE.fullmatch(line) is None
    )
    path.write_text(
        f"# managed-metadata-sha256: {_managed_metadata_digest()}\n{without_old_digest}\n",
        encoding="utf-8",
    )


def _check_metadata_digest(content: str, path: Path) -> None:
    first_line = content.splitlines()[0] if content else ""
    match = METADATA_DIGEST_LINE.fullmatch(first_line)
    expected = _managed_metadata_digest()
    if match is None or match.group(1) != expected:
        raise RuntimeError(
            f"{path.name} does not match managed package metadata; regenerate managed runtime locks"
        )


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"managed runtime lock error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
