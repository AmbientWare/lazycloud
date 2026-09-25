from __future__ import annotations

import importlib
import io
import json
import tarfile
from hashlib import sha256
from pathlib import Path

import pytest

build_agent_binaries = importlib.import_module("deploy.agent-binary.build")


def _release(path: Path, payload: bytes) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        executable = tarfile.TarInfo("./lazycloud-agent")
        executable.size = len(payload)
        executable.mode = 0o755
        archive.addfile(executable, io.BytesIO(payload))
    return path


def test_stage_agent_binaries_writes_versioned_immutable_layout(tmp_path: Path) -> None:
    amd64 = _release(tmp_path / "amd64", b"amd64")
    arm64 = _release(tmp_path / "arm64", b"arm64")
    output = tmp_path / "published"

    manifest = build_agent_binaries.stage_artifacts(
        version="2026.07.14",
        output_root=output,
        artifact_paths={"amd64": amd64, "arm64": arm64},
    )

    version_root = output / "2026.07.14"
    assert (version_root / "lazycloud-agent-linux-amd64.tar.gz").read_bytes() == amd64.read_bytes()
    assert (version_root / "lazycloud-agent-linux-arm64.tar.gz").read_bytes() == arm64.read_bytes()
    assert json.loads((version_root / "manifest.json").read_text()) == manifest
    assert {artifact["arch"]: artifact["sha256"] for artifact in manifest["artifacts"]} == {
        "amd64": sha256(amd64.read_bytes()).hexdigest(),
        "arm64": sha256(arm64.read_bytes()).hexdigest(),
    }


def test_stage_agent_binaries_rejects_mutating_a_published_version(tmp_path: Path) -> None:
    output = tmp_path / "published"
    first = _release(tmp_path / "first", b"first")
    changed = _release(tmp_path / "changed", b"changed")
    build_agent_binaries.stage_artifacts(
        version="v1",
        output_root=output,
        artifact_paths={"amd64": first},
    )

    with pytest.raises(RuntimeError, match="immutable agent artifact already exists"):
        build_agent_binaries.stage_artifacts(
            version="v1",
            output_root=output,
            artifact_paths={"amd64": changed},
        )


def test_stage_agent_binaries_rejects_an_archive_without_the_agent(tmp_path: Path) -> None:
    payload = tmp_path / "executable"
    payload.write_bytes(b"\x7fELF agent")

    with pytest.raises(RuntimeError, match="not an agent release archive"):
        build_agent_binaries.stage_artifacts(
            version="v1",
            output_root=tmp_path / "published",
            artifact_paths={"amd64": payload},
        )
