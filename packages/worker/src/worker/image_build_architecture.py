from __future__ import annotations

import platform
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from shared.image_building.authoring import LinuxArchitecture

_BINFMT_ROOT = Path("/proc/sys/fs/binfmt_misc")
_BINFMT_BUNDLE_ROOT = Path("/opt/lazycloud/binfmt")
_BINFMT_REGISTRATION_LOCK = threading.Lock()


class ImageBuildArchitectureError(RuntimeError):
    """Raised when this worker cannot execute a requested foreign architecture."""


class ImageBuildArchitecturePreparer(Protocol):
    def ensure(self, target: LinuxArchitecture) -> None: ...


type BinfmtMounter = Callable[[Path], None]
type BinfmtRegistrationWriter = Callable[[Path, str], None]
type HostMachineResolver = Callable[[], str]


@dataclass(frozen=True, slots=True)
class _ForeignArchitecture:
    handler_name: str
    emulator_name: str
    config_name: str


_FOREIGN_ARCHITECTURES = {
    LinuxArchitecture.Amd64: _ForeignArchitecture(
        handler_name="qemu-x86_64",
        emulator_name="qemu-x86_64-static",
        config_name="qemu-x86_64.conf",
    ),
    LinuxArchitecture.Arm64: _ForeignArchitecture(
        handler_name="qemu-aarch64",
        emulator_name="qemu-aarch64-static",
        config_name="qemu-aarch64.conf",
    ),
}


def _mount_binfmt_misc(root: Path) -> None:
    mount = shutil.which("mount")
    if mount is None:
        raise ImageBuildArchitectureError(
            "cannot mount binfmt_misc because the mount binary is unavailable"
        )
    process = subprocess.run(
        [mount, "--types", "binfmt_misc", "binfmt_misc", str(root)],
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode == 0:
        return
    detail = (process.stderr or process.stdout).strip() or f"exit {process.returncode}"
    raise ImageBuildArchitectureError(f"cannot mount binfmt_misc: {detail}")


def _write_binfmt_registration(register: Path, registration: str) -> None:
    register.write_text(registration, encoding="utf-8")


@dataclass(frozen=True, slots=True)
class ImageBuildArchitectureRuntime:
    binfmt_root: Path = _BINFMT_ROOT
    bundle_root: Path = _BINFMT_BUNDLE_ROOT
    host_machine: HostMachineResolver = platform.machine
    mount_binfmt: BinfmtMounter = _mount_binfmt_misc
    write_registration: BinfmtRegistrationWriter = _write_binfmt_registration

    def ensure(self, target: LinuxArchitecture) -> None:
        host = _linux_architecture(self.host_machine())
        if host is target:
            return

        foreign = _FOREIGN_ARCHITECTURES[target]
        handler = self.binfmt_root / foreign.handler_name
        if handler.exists():
            _require_usable_handler(handler, target=target)
            return

        emulator = self.bundle_root / foreign.emulator_name
        config = self.bundle_root / foreign.config_name
        registration = _load_registration(
            config,
            emulator=emulator,
            handler_name=foreign.handler_name,
            target=target,
        )

        with _BINFMT_REGISTRATION_LOCK:
            if handler.exists():
                _require_usable_handler(handler, target=target)
                return
            register = self.binfmt_root / "register"
            if not register.exists():
                try:
                    self.mount_binfmt(self.binfmt_root)
                except ImageBuildArchitectureError as exc:
                    raise ImageBuildArchitectureError(_privilege_error(target, str(exc))) from exc
                except OSError as exc:
                    raise ImageBuildArchitectureError(
                        _privilege_error(target, f"{type(exc).__name__}: {exc}")
                    ) from exc
            if not register.exists():
                raise ImageBuildArchitectureError(
                    _privilege_error(target, "binfmt_misc register file is unavailable")
                )
            try:
                self.write_registration(register, registration)
            except OSError as exc:
                if handler.exists():
                    _require_usable_handler(handler, target=target)
                    return
                raise ImageBuildArchitectureError(
                    _privilege_error(target, f"{type(exc).__name__}: {exc}")
                ) from exc
            if not handler.exists():
                raise ImageBuildArchitectureError(
                    _privilege_error(
                        target,
                        f"kernel did not create the {foreign.handler_name} handler",
                    )
                )
            _require_usable_handler(handler, target=target)


def _linux_architecture(machine: str) -> LinuxArchitecture:
    normalized = machine.strip().lower()
    if normalized in {"amd64", "x86_64"}:
        return LinuxArchitecture.Amd64
    if normalized in {"arm64", "aarch64"}:
        return LinuxArchitecture.Arm64
    raise ImageBuildArchitectureError(
        f"image builds are unsupported on worker architecture {machine or '<empty>'!r}; "
        "supported worker architectures are amd64 and arm64"
    )


def _load_registration(
    config: Path,
    *,
    emulator: Path,
    handler_name: str,
    target: LinuxArchitecture,
) -> str:
    if not emulator.is_file() or not emulator.stat().st_mode & 0o111:
        raise ImageBuildArchitectureError(
            f"{target.value} image builds require the bundled static emulator {emulator}"
        )
    try:
        registration = config.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ImageBuildArchitectureError(
            f"{target.value} image builds require the bundled binfmt configuration {config}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    fields = registration.split(":")
    if (
        len(fields) != 8
        or fields[0]
        or fields[1] != handler_name
        or fields[6] != str(emulator)
        or "F" not in fields[7]
    ):
        raise ImageBuildArchitectureError(
            f"bundled binfmt configuration is invalid for {target.value}: {config}"
        )
    return registration


def _require_usable_handler(handler: Path, *, target: LinuxArchitecture) -> None:
    try:
        status = handler.read_text(encoding="utf-8")
    except OSError as exc:
        raise ImageBuildArchitectureError(
            f"cannot inspect existing {target.value} binfmt handler {handler}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    lines = status.splitlines()
    flags = next(
        (line.removeprefix("flags:").strip() for line in lines if line.startswith("flags:")),
        "",
    )
    if lines and lines[0] == "enabled" and "F" in flags:
        return
    raise ImageBuildArchitectureError(
        f"existing {target.value} binfmt handler {handler} is disabled or lacks "
        "the persistent-interpreter F flag"
    )


def _privilege_error(target: LinuxArchitecture, detail: str) -> str:
    return (
        f"{target.value} image builds on this worker require writable binfmt_misc support; "
        "run the worker with the documented privileged container configuration "
        f"({detail})"
    )


__all__ = [
    "ImageBuildArchitectureError",
    "ImageBuildArchitecturePreparer",
    "ImageBuildArchitectureRuntime",
]
