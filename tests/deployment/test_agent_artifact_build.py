from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

build_agent_artifacts = importlib.import_module("deploy.agent-artifact.build")


def _elf(path: Path, payload: bytes) -> Path:
    path.write_bytes(b"\x7fELF" + payload)
    return path


def test_stage_agent_artifacts_writes_versioned_immutable_layout(tmp_path: Path) -> None:
    amd64 = _elf(tmp_path / "amd64", b"amd64")
    arm64 = _elf(tmp_path / "arm64", b"arm64")
    output = tmp_path / "published"

    manifest = build_agent_artifacts.stage_artifacts(
        version="2026.07.14",
        output_root=output,
        artifact_paths={"amd64": amd64, "arm64": arm64},
    )

    version_root = output / "2026.07.14"
    assert (version_root / "lazycloud-agent-linux-amd64").read_bytes() == amd64.read_bytes()
    assert (version_root / "lazycloud-agent-linux-arm64").read_bytes() == arm64.read_bytes()
    assert json.loads((version_root / "manifest.json").read_text()) == manifest
    assert [artifact["arch"] for artifact in manifest["artifacts"]] == ["amd64", "arm64"]
    assert all(len(artifact["sha256"]) == 64 for artifact in manifest["artifacts"])


def test_stage_agent_artifacts_rejects_mutating_a_published_version(tmp_path: Path) -> None:
    output = tmp_path / "published"
    first = _elf(tmp_path / "first", b"first")
    changed = _elf(tmp_path / "changed", b"changed")
    build_agent_artifacts.stage_artifacts(
        version="v1",
        output_root=output,
        artifact_paths={"amd64": first},
    )

    with pytest.raises(RuntimeError, match="immutable agent artifact already exists"):
        build_agent_artifacts.stage_artifacts(
            version="v1",
            output_root=output,
            artifact_paths={"amd64": changed},
        )


def test_stage_agent_artifacts_rejects_non_linux_payload(tmp_path: Path) -> None:
    payload = tmp_path / "script"
    payload.write_text("#!/bin/sh\n")

    with pytest.raises(RuntimeError, match="not a Linux ELF executable"):
        build_agent_artifacts.stage_artifacts(
            version="v1",
            output_root=tmp_path / "published",
            artifact_paths={"amd64": payload},
        )
