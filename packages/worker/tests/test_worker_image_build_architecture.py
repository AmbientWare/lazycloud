from __future__ import annotations

from pathlib import Path

import pytest
from shared.image_building.authoring import LinuxArchitecture
from worker.image_build_architecture import (
    ImageBuildArchitectureError,
    ImageBuildArchitectureRuntime,
)


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
