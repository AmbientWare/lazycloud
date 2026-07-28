from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from shared.image_building.authoring import LinuxArchitecture
from worker.container_checkpoints import ContainerImageArchiveResult
from worker.image_build_architecture import (
    ImageBuildArchitectureError,
    ImageBuildArchitectureRuntime,
)
from worker.image_build_execution import (
    BuildahWorkerImageBuilder,
    ImageBuildLog,
    WorkerImageBuildRequestPayload,
)
from worker.image_build_scratch import ImageBuildScratchLease, ImageBuildScratchManager
from worker.image_lifecycle import BuildahDirectoryPlan, BuildahStorageDriver

from worker import image_build_execution


@dataclass(slots=True)
class _RecordingArchitecturePreparer:
    events: list[str] = field(default_factory=list)

    def ensure(self, target: LinuxArchitecture) -> None:
        self.events.append(f"prepare:{target.value}")


class _SuccessfulImageArchiver:
    def __init__(self, archive_path: Path) -> None:
        self.archive_path = archive_path

    def archive_image(
        self,
        source_path: Path,
        image_id: str,
        progress: Callable[[int], None],
    ) -> ContainerImageArchiveResult:
        del source_path, image_id, progress
        self.archive_path.write_bytes(b"archive")
        return ContainerImageArchiveResult(
            success=True,
            archive_path=str(self.archive_path),
        )


@pytest.mark.parametrize(
    "build_options",
    [
        {"dockerfile": "FROM scratch\n"},
        {"source_image": "docker.io/library/python:3.12-slim"},
    ],
)
def test_buildah_targets_requested_architecture_for_build_and_from_operations(
    build_options: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    commands: list[list[str]] = []
    mounted_root = tmp_path / "mounted-root"
    mounted_root.mkdir()
    preparer = _RecordingArchitecturePreparer(events)

    def run_buildah(
        _self: BuildahWorkerImageBuilder,
        args: Sequence[str],
        *,
        directories: BuildahDirectoryPlan,
        driver: BuildahStorageDriver,
        env: dict[str, str],
        cwd: Path,
        log: ImageBuildLog,
        capture_stdout: bool = False,
        scratch: ImageBuildScratchLease | None = None,
    ) -> str:
        del directories, driver, env, cwd, log, scratch
        command = list(args)
        events.append(f"buildah:{command[0]}")
        commands.append(command)
        if command[0] == "mount" and capture_stdout:
            return str(mounted_root)
        return ""

    def buildah_path(_binary: str) -> str:
        return "/usr/bin/buildah"

    monkeypatch.setattr(image_build_execution.shutil, "which", buildah_path)
    monkeypatch.setattr(BuildahWorkerImageBuilder, "_run_buildah", run_buildah)
    builder = BuildahWorkerImageBuilder(
        archiver=_SuccessfulImageArchiver(tmp_path / "image.rclip"),
        scratch=ImageBuildScratchManager(
            root=tmp_path / "build-root",
            worker_id="worker-1",
            max_bytes=32 * 1024 * 1024,
            per_build_max_bytes=16 * 1024 * 1024,
            minimum_free_bytes=0,
            buildah_binary="cleanup-buildah-missing",
        ),
        architecture_preparer=preparer,
        storage_driver=BuildahStorageDriver.Vfs,
        fallback_storage_driver=BuildahStorageDriver.Vfs,
    )
    payload = WorkerImageBuildRequestPayload.model_validate(
        {
            "build_id": "build-1",
            "image_id": "image-1",
            "build_options": {
                **build_options,
                "architecture": "amd64",
                "managed_package_digest": "a" * 64,
            },
        }
    )

    result = builder.build_image_archive(
        payload,
        container_id="container-1",
        log=lambda _message: None,
    )

    assert result.ok
    assert events[0] == "prepare:amd64"
    assert events[1].startswith("buildah:")
    targeted_commands = [command for command in commands if command[0] in {"bud", "from"}]
    assert targeted_commands
    assert all(command[1:3] == ["--arch", "amd64"] for command in targeted_commands)


def test_native_image_build_architecture_requires_no_binfmt_support(tmp_path: Path) -> None:
    def unexpected_mount(_root: Path) -> None:
        raise AssertionError("native image build tried to mount binfmt_misc")

    def unexpected_registration(_register: Path, _registration: str) -> None:
        raise AssertionError("native image build tried to register binfmt")

    runtime = ImageBuildArchitectureRuntime(
        binfmt_root=tmp_path / "missing-binfmt",
        bundle_root=tmp_path / "missing-bundle",
        host_machine=lambda: "aarch64",
        mount_binfmt=unexpected_mount,
        write_registration=unexpected_registration,
    )

    runtime.ensure(LinuxArchitecture.Arm64)


@pytest.mark.parametrize(
    ("host", "target", "qemu_arch"),
    [
        ("arm64", LinuxArchitecture.Amd64, "x86_64"),
        ("x86_64", LinuxArchitecture.Arm64, "aarch64"),
    ],
)
def test_cross_architecture_registers_bundled_persistent_handler(
    tmp_path: Path,
    host: str,
    target: LinuxArchitecture,
    qemu_arch: str,
) -> None:
    binfmt_root = tmp_path / "binfmt"
    bundle_root = tmp_path / "bundle"
    binfmt_root.mkdir()
    bundle_root.mkdir()
    register = binfmt_root / "register"
    register.touch()
    emulator = bundle_root / f"qemu-{qemu_arch}-static"
    emulator.write_bytes(b"static emulator")
    emulator.chmod(0o755)
    handler_name = f"qemu-{qemu_arch}"
    registration = f":{handler_name}:M::magic:mask:{emulator}:OPF"
    (bundle_root / f"{handler_name}.conf").write_text(registration, encoding="utf-8")
    writes: list[tuple[Path, str]] = []

    def write_registration(path: Path, value: str) -> None:
        writes.append((path, value))
        (binfmt_root / handler_name).write_text(
            f"enabled\ninterpreter {emulator}\nflags: POF\n",
            encoding="utf-8",
        )

    runtime = ImageBuildArchitectureRuntime(
        binfmt_root=binfmt_root,
        bundle_root=bundle_root,
        host_machine=lambda: host,
        mount_binfmt=lambda _root: None,
        write_registration=write_registration,
    )

    runtime.ensure(target)

    assert writes == [(register, registration)]


def test_cross_architecture_reuses_existing_persistent_handler(tmp_path: Path) -> None:
    binfmt_root = tmp_path / "binfmt"
    binfmt_root.mkdir()
    (binfmt_root / "qemu-aarch64").write_text(
        "enabled\ninterpreter /already-opened/qemu-aarch64\nflags: POCF\n",
        encoding="utf-8",
    )

    def unexpected_mount(_root: Path) -> None:
        raise AssertionError("existing handler tried to mount binfmt_misc")

    def unexpected_registration(_register: Path, _registration: str) -> None:
        raise AssertionError("existing handler tried to register binfmt")

    runtime = ImageBuildArchitectureRuntime(
        binfmt_root=binfmt_root,
        bundle_root=tmp_path / "missing-bundle",
        host_machine=lambda: "x86_64",
        mount_binfmt=unexpected_mount,
        write_registration=unexpected_registration,
    )

    runtime.ensure(LinuxArchitecture.Arm64)


def test_cross_architecture_reports_missing_worker_privilege(tmp_path: Path) -> None:
    binfmt_root = tmp_path / "binfmt"
    bundle_root = tmp_path / "bundle"
    binfmt_root.mkdir()
    bundle_root.mkdir()
    emulator = bundle_root / "qemu-x86_64-static"
    emulator.write_bytes(b"static emulator")
    emulator.chmod(0o755)
    (bundle_root / "qemu-x86_64.conf").write_text(
        f":qemu-x86_64:M::magic:mask:{emulator}:OPF",
        encoding="utf-8",
    )

    def denied_mount(_root: Path) -> None:
        raise PermissionError("operation not permitted")

    runtime = ImageBuildArchitectureRuntime(
        binfmt_root=binfmt_root,
        bundle_root=bundle_root,
        host_machine=lambda: "aarch64",
        mount_binfmt=denied_mount,
    )

    with pytest.raises(ImageBuildArchitectureError, match="privileged container") as exc:
        runtime.ensure(LinuxArchitecture.Amd64)

    assert "operation not permitted" in str(exc.value)
