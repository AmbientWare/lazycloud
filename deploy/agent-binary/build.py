"""Build immutable standalone agent artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

SCHEMA_VERSION = 1
SUPPORTED_ARCHITECTURES = ("amd64", "arm64")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class AgentArtifactPayload(TypedDict):
    os: str
    arch: str
    filename: str
    sha256: str
    size_bytes: int


class AgentArtifactManifest(TypedDict):
    schema_version: int
    version: str
    artifacts: list[AgentArtifactPayload]


@dataclass(frozen=True, slots=True)
class AgentArtifact:
    os: str
    arch: str
    filename: str
    sha256: str
    size_bytes: int

    def payload(self) -> AgentArtifactPayload:
        return {
            "os": self.os,
            "arch": self.arch,
            "filename": self.filename,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build and stage immutable standalone Linux agent artifacts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--version", required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument(
        "--arch",
        choices=SUPPORTED_ARCHITECTURES,
        action="append",
        dest="architectures",
    )
    build.add_argument("--dockerfile", type=Path, default=Path("deploy/agent-binary/Dockerfile"))
    build.add_argument("--context", type=Path, default=Path("."))

    stage = subparsers.add_parser("stage")
    stage.add_argument("--version", required=True)
    stage.add_argument("--output", type=Path, required=True)
    stage.add_argument(
        "--artifact",
        action="append",
        required=True,
        metavar="ARCH=PATH",
    )

    args = parser.parse_args()
    if args.command == "build":
        architectures = _unique_architectures(args.architectures or SUPPORTED_ARCHITECTURES)
        build_artifacts(
            version=args.version,
            output_root=args.output,
            architectures=architectures,
            dockerfile=args.dockerfile,
            context=args.context,
        )
        return
    artifact_paths = _parse_artifact_arguments(args.artifact)
    manifest = stage_artifacts(
        version=args.version,
        output_root=args.output,
        artifact_paths=artifact_paths,
    )
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))


def build_artifacts(
    *,
    version: str,
    output_root: Path,
    architectures: tuple[str, ...],
    dockerfile: Path,
    context: Path,
) -> AgentArtifactManifest:
    _validate_version(version)
    dockerfile = dockerfile.resolve()
    context = context.resolve()
    if not dockerfile.is_file():
        raise RuntimeError(f"agent artifact Dockerfile does not exist: {dockerfile}")
    if not context.is_dir():
        raise RuntimeError(f"agent artifact build context does not exist: {context}")

    with tempfile.TemporaryDirectory(prefix="lazycloud-agent-binarys-") as temporary:
        temporary_root = Path(temporary)
        artifacts: dict[str, Path] = {}
        for architecture in architectures:
            export_root = temporary_root / architecture
            command = docker_build_command(
                architecture=architecture,
                dockerfile=dockerfile,
                context=context,
                export_root=export_root,
            )
            subprocess.run(command, check=True)
            artifact = export_root / artifact_filename(architecture)
            if not artifact.is_file():
                raise RuntimeError(
                    f"Docker did not export the {architecture} agent artifact: {artifact}"
                )
            artifacts[architecture] = artifact
        manifest = stage_artifacts(
            version=version,
            output_root=output_root,
            artifact_paths=artifacts,
        )
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    return manifest


def docker_build_command(
    *,
    architecture: str,
    dockerfile: Path,
    context: Path,
    export_root: Path,
) -> list[str]:
    _validate_architecture(architecture)
    return [
        "docker",
        "buildx",
        "build",
        "--platform",
        f"linux/{architecture}",
        "--file",
        str(dockerfile),
        "--target",
        "agent-binary",
        "--output",
        f"type=local,dest={export_root}",
        str(context),
    ]


def stage_artifacts(
    *,
    version: str,
    output_root: Path,
    artifact_paths: dict[str, Path],
) -> AgentArtifactManifest:
    _validate_version(version)
    if not artifact_paths:
        raise ValueError("at least one agent artifact is required")
    unexpected = sorted(set(artifact_paths) - set(SUPPORTED_ARCHITECTURES))
    if unexpected:
        raise ValueError(f"unsupported agent artifact architectures: {', '.join(unexpected)}")

    version_root = output_root.resolve() / version
    version_root.mkdir(parents=True, exist_ok=True)
    artifact_metadata: list[AgentArtifact] = []
    for architecture in SUPPORTED_ARCHITECTURES:
        source = artifact_paths.get(architecture)
        if source is None:
            continue
        source = source.resolve()
        if not source.is_file():
            raise RuntimeError(f"agent artifact does not exist: {source}")
        if not _is_elf(source):
            raise RuntimeError(f"agent artifact is not a Linux ELF executable: {source}")
        filename = artifact_filename(architecture)
        destination = version_root / filename
        _copy_immutable(source, destination)
        destination.chmod(0o755)
        artifact_metadata.append(
            AgentArtifact(
                os="linux",
                arch=architecture,
                filename=filename,
                sha256=_sha256(destination),
                size_bytes=destination.stat().st_size,
            )
        )

    manifest: AgentArtifactManifest = {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "artifacts": [artifact.payload() for artifact in artifact_metadata],
    }
    manifest_path = version_root / "manifest.json"
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    _write_immutable(manifest_path, payload.encode())
    return manifest


def artifact_filename(architecture: str) -> str:
    _validate_architecture(architecture)
    return f"lazycloud-agent-linux-{architecture}"


def _parse_artifact_arguments(arguments: list[str]) -> dict[str, Path]:
    artifacts: dict[str, Path] = {}
    for argument in arguments:
        architecture, separator, raw_path = argument.partition("=")
        if not separator or not raw_path:
            raise ValueError("agent artifacts must use ARCH=PATH")
        _validate_architecture(architecture)
        if architecture in artifacts:
            raise ValueError(f"duplicate agent artifact architecture: {architecture}")
        artifacts[architecture] = Path(raw_path)
    return artifacts


def _unique_architectures(architectures: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    unique: list[str] = []
    for architecture in architectures:
        _validate_architecture(architecture)
        if architecture not in unique:
            unique.append(architecture)
    return tuple(unique)


def _validate_architecture(architecture: str) -> None:
    if architecture not in SUPPORTED_ARCHITECTURES:
        raise ValueError(f"unsupported agent artifact architecture: {architecture}")


def _validate_version(version: str) -> None:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            "agent artifact version must contain only letters, numbers, dot, dash, or underscore"
        )


def _is_elf(path: Path) -> bool:
    with path.open("rb") as artifact:
        return artifact.read(4) == b"\x7fELF"


def _copy_immutable(source: Path, destination: Path) -> None:
    if destination.exists():
        if not destination.is_file() or _sha256(source) != _sha256(destination):
            raise RuntimeError(
                f"immutable agent artifact already exists with different bytes: {destination}"
            )
        return
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _write_immutable(destination: Path, payload: bytes) -> None:
    if destination.exists():
        if destination.read_bytes() != payload:
            raise RuntimeError(f"immutable agent artifact manifest already exists: {destination}")
        return
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
