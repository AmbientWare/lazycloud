from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from typing import BinaryIO

import pytest
from agent.updates import (
    RELEASE_COMPLETE_FILE,
    SUPERVISOR,
    UPDATE_PENDING_FILE,
    AgentUpdater,
    AgentUpdateRestartError,
)
from shared.app_identity import AGENT_NAME
from shared.releases import AgentArtifact

from agent import updates


class _Restarted(Exception):
    pass


def _release(path: Path, label: str) -> AgentArtifact:
    payload = f"#!/bin/sh\necho {label}\n".encode()
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(f"./{AGENT_NAME}")
        member.size = len(payload)
        member.mode = 0o755
        archive.addfile(member, io.BytesIO(payload))
    data = path.read_bytes()
    return AgentArtifact(
        url=f"https://releases.test/{path.name}",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )


def _installed(root: Path, artifact: AgentArtifact) -> Path:
    release = root / "lib" / AGENT_NAME / artifact.sha256
    release.mkdir(parents=True)
    with tarfile.open(root.parent / artifact.url.rsplit("/", 1)[1], "r:gz") as archive:
        archive.extractall(release, filter="data")
    command = root / "bin" / AGENT_NAME
    command.parent.mkdir()
    command.symlink_to(release / AGENT_NAME)
    return command


def test_an_update_switches_the_command_and_keeps_only_the_releases_it_can_return_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def download(url: str, *, timeout: float) -> BinaryIO:
        del timeout
        return (tmp_path / url.rsplit("/", 1)[1]).open("rb")

    monkeypatch.setattr(updates.urllib.request, "urlopen", download)
    state = tmp_path / "state"
    state.mkdir()
    (state / SUPERVISOR).write_text("")
    first = _release(tmp_path / "first.tar.gz", "first")
    second = _release(tmp_path / "second.tar.gz", "second")
    third = _release(tmp_path / "third.tar.gz", "third")
    command = _installed(tmp_path / "prefix", first)
    releases = command.parent.parent / "lib" / AGENT_NAME

    def restart() -> None:
        raise _Restarted

    for artifact in (second, third):
        with pytest.raises(AgentUpdateRestartError):
            AgentUpdater(command, state, command.resolve()).install(artifact, before_exec=restart)
        updated = AgentUpdater(command, state, command.resolve())
        assert updated.binary_sha256() == artifact.sha256
        assert (state / UPDATE_PENDING_FILE).read_text().strip() == artifact.sha256
        updated.confirm()

    assert not (state / UPDATE_PENDING_FILE).exists()
    assert command.with_name(f"{AGENT_NAME}.previous").resolve().parent.name == second.sha256
    assert sorted(entry.name for entry in releases.iterdir()) == sorted(
        [second.sha256, third.sha256]
    )

    tampered = third.model_copy(update={"sha256": "0" * 64})
    with pytest.raises(RuntimeError, match="does not match"):
        AgentUpdater(command, state, command.resolve()).install(tampered, before_exec=restart)
    assert AgentUpdater(command, state, command.resolve()).binary_sha256() == third.sha256
    assert sorted(entry.name for entry in releases.iterdir()) == sorted(
        [second.sha256, third.sha256]
    )


def test_a_release_left_incomplete_is_unpacked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A digest directory without its completion mark, as a cut-short removal leaves, goes."""

    def download(url: str, *, timeout: float) -> BinaryIO:
        del timeout
        return (tmp_path / url.rsplit("/", 1)[1]).open("rb")

    def restart() -> None:
        raise _Restarted

    monkeypatch.setattr(updates.urllib.request, "urlopen", download)
    state = tmp_path / "state"
    state.mkdir()
    (state / SUPERVISOR).write_text("")
    first = _release(tmp_path / "first.tar.gz", "first")
    second = _release(tmp_path / "second.tar.gz", "second")
    command = _installed(tmp_path / "prefix", first)
    incomplete = command.parent.parent / "lib" / AGENT_NAME / second.sha256
    incomplete.mkdir()

    with pytest.raises(AgentUpdateRestartError):
        AgentUpdater(command, state, command.resolve()).install(second, before_exec=restart)

    assert (incomplete / AGENT_NAME).is_file()
    assert (incomplete / RELEASE_COMPLETE_FILE).is_file()
    assert AgentUpdater(command, state, command.resolve()).binary_sha256() == second.sha256
