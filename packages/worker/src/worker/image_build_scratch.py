from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import threading
import tomllib
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import IO
from uuid import uuid4

from pydantic import Field
from shared.contracts import ContractModel
from shared.timestamps import utc_now

from worker.image_lifecycle import (
    BuildahDirectoryPlan,
    BuildahStorageDriver,
    buildah_environment,
    plan_buildah_directories,
)

DEFAULT_IMAGE_BUILD_ROOT = Path("/var/lib/lazycloud/builds")
DEFAULT_IMAGE_BUILD_SCRATCH_MAX_BYTES = 32 * 1024 * 1024 * 1024
DEFAULT_IMAGE_BUILD_PER_BUILD_MAX_BYTES = 16 * 1024 * 1024 * 1024
DEFAULT_IMAGE_BUILD_MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024
DEFAULT_IMAGE_BUILD_STALE_SECONDS = 30 * 60
IMAGE_BUILD_SCRATCH_PREFIX = "image-build-"
IMAGE_BUILD_SCRATCH_MONITOR_SECONDS = 1.0


class ImageBuildScratchCapacityError(RuntimeError):
    pass


class _BuildahStorageSection(ContractModel):
    driver: BuildahStorageDriver


class _BuildahStorageDocument(ContractModel):
    storage: _BuildahStorageSection


class ImageBuildScratchReconcileResult(ContractModel):
    scanned: int = 0
    active: int = 0
    recent: int = 0
    removed: int = 0
    freed_bytes: int = 0
    cleanup_failures: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class ImageBuildScratchLease:
    root: Path
    reservation_bytes: int
    minimum_free_bytes: int
    _lease_file: IO[str]
    _peak_bytes: int = 0
    _peak_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def peak_bytes(self) -> int:
        with self._peak_lock:
            return self._peak_bytes

    def check_capacity(self) -> None:
        used = allocated_path_bytes(self.root)
        with self._peak_lock:
            self._peak_bytes = max(self._peak_bytes, used)
        if used > self.reservation_bytes:
            raise ImageBuildScratchCapacityError(
                "image build scratch exceeded its per-build limit: "
                f"used={used} limit={self.reservation_bytes}"
            )
        free = shutil.disk_usage(self.root).free
        if free < self.minimum_free_bytes:
            raise ImageBuildScratchCapacityError(
                "image build scratch crossed the filesystem free-space reserve: "
                f"free={free} required={self.minimum_free_bytes}"
            )

    def close(self) -> None:
        lease_file = self._lease_file
        if not getattr(lease_file, "closed", True):
            fcntl.flock(lease_file.fileno(), fcntl.LOCK_UN)
            lease_file.close()


@dataclass(slots=True)
class ImageBuildScratchManager:
    root: Path
    worker_id: str
    max_bytes: int = DEFAULT_IMAGE_BUILD_SCRATCH_MAX_BYTES
    per_build_max_bytes: int = DEFAULT_IMAGE_BUILD_PER_BUILD_MAX_BYTES
    minimum_free_bytes: int = DEFAULT_IMAGE_BUILD_MIN_FREE_BYTES
    stale_seconds: int = DEFAULT_IMAGE_BUILD_STALE_SECONDS
    buildah_binary: str = "buildah"

    def __post_init__(self) -> None:
        self.root = self.root.expanduser().absolute()
        if self.root == Path("/"):
            raise ValueError("image build scratch root cannot be the filesystem root")
        if self.max_bytes <= 0 or self.per_build_max_bytes <= 0:
            raise ValueError("image build scratch limits must be positive")
        if self.per_build_max_bytes > self.max_bytes:
            raise ValueError("per-build scratch limit cannot exceed aggregate scratch limit")
        if self.minimum_free_bytes < 0 or self.stale_seconds < 0:
            raise ValueError("image build scratch reserve and stale interval cannot be negative")

    def acquire(self, *, build_id: str, container_id: str) -> ImageBuildScratchLease:
        root = self._prepare_root()
        with _LockedFile(root / ".capacity.lock"):
            reserved = _reserved_bytes(root)
            if reserved + self.per_build_max_bytes > self.max_bytes:
                raise ImageBuildScratchCapacityError(
                    "image build scratch capacity is exhausted: "
                    f"reserved={reserved} requested={self.per_build_max_bytes} "
                    f"limit={self.max_bytes}"
                )
            free = shutil.disk_usage(root).free
            required_free = self.minimum_free_bytes + self.per_build_max_bytes
            if free < required_free:
                raise ImageBuildScratchCapacityError(
                    "image build scratch has insufficient free space: "
                    f"free={free} required={required_free}"
                )
            build_root = root / _build_root_name(
                worker_id=self.worker_id,
                build_id=build_id,
                container_id=container_id,
            )
            build_root.mkdir(mode=0o700)
            lease_file: IO[str] | None = None
            try:
                reservation = build_root / ".reservation"
                reservation.write_text(f"{self.per_build_max_bytes}\n", encoding="ascii")
                reservation.chmod(0o600)
                lease_file = (build_root / ".lease").open("a+", encoding="ascii")
                lease_file.write(f"pid={os.getpid()}\n")
                lease_file.flush()
                os.fsync(lease_file.fileno())
                fcntl.flock(lease_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception:
                if lease_file is not None:
                    lease_file.close()
                shutil.rmtree(build_root, ignore_errors=True)
                raise
            if lease_file is None:
                raise RuntimeError("image build scratch lease was not created")
        return ImageBuildScratchLease(
            root=build_root,
            reservation_bytes=self.per_build_max_bytes,
            minimum_free_bytes=self.minimum_free_bytes,
            _lease_file=lease_file,
        )

    def release(self, lease: ImageBuildScratchLease) -> None:
        try:
            self._validate_build_root(lease.root)
            shutil.rmtree(lease.root)
        finally:
            lease.close()

    def cleanup_store(
        self,
        root: Path,
        *,
        driver: BuildahStorageDriver,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        self._validate_build_root(root)
        return cleanup_isolated_buildah_store(
            self.buildah_binary,
            plan_buildah_directories(str(root)),
            driver=driver,
            env=env,
        )

    def reconcile(self, *, now: datetime | None = None) -> ImageBuildScratchReconcileResult:
        current = now or utc_now()
        root = self._prepare_root()
        cutoff = current - timedelta(seconds=self.stale_seconds)
        scanned = 0
        active = 0
        recent = 0
        removed = 0
        freed = 0
        failures: list[str] = []
        for candidate in sorted(root.iterdir(), key=lambda item: item.name):
            if not candidate.is_dir() or not candidate.name.startswith(IMAGE_BUILD_SCRATCH_PREFIX):
                continue
            scanned += 1
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=current.tzinfo)
            if modified > cutoff:
                recent += 1
                continue
            lease_path = candidate / ".lease"
            lease_path.touch(mode=0o600, exist_ok=True)
            lease_file = lease_path.open("a+", encoding="ascii")
            try:
                try:
                    fcntl.flock(lease_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    active += 1
                    continue
                size = allocated_path_bytes(candidate)
                driver = _stored_driver(candidate)
                if driver is not None:
                    failures.extend(self.cleanup_store(candidate, driver=driver))
                try:
                    shutil.rmtree(candidate)
                except OSError as exc:
                    failures.append(f"{candidate.name}: {type(exc).__name__}: {exc}")
                    continue
                removed += 1
                freed += size
            finally:
                with suppress(OSError):
                    fcntl.flock(lease_file.fileno(), fcntl.LOCK_UN)
                lease_file.close()
        return ImageBuildScratchReconcileResult(
            scanned=scanned,
            active=active,
            recent=recent,
            removed=removed,
            freed_bytes=freed,
            cleanup_failures=failures,
        )

    def _prepare_root(self) -> Path:
        if self.root.is_symlink():
            raise ValueError("image build scratch root cannot be a symlink")
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        resolved = self.root.resolve(strict=True)
        if resolved != self.root:
            raise ValueError("image build scratch root cannot traverse symlinks")
        resolved.chmod(0o700)
        flags = os.statvfs(resolved).f_flag
        noexec_flag = getattr(os, "ST_NOEXEC", 0)
        if noexec_flag and flags & noexec_flag:
            raise ValueError("image build scratch root must be on an executable filesystem")
        probe = resolved / f".write-probe-{uuid4().hex}"
        descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, b"ready\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
            probe.unlink(missing_ok=True)
        return resolved

    def _validate_build_root(self, build_root: Path) -> None:
        resolved_root = self._prepare_root()
        resolved_build_root = build_root.resolve(strict=True)
        if resolved_build_root.parent != resolved_root or not resolved_build_root.name.startswith(
            IMAGE_BUILD_SCRATCH_PREFIX
        ):
            raise ValueError("image build scratch path is not owned by this manager")


def cleanup_isolated_buildah_store(
    binary: str,
    directories: BuildahDirectoryPlan,
    *,
    driver: BuildahStorageDriver,
    env: dict[str, str] | None = None,
) -> list[str]:
    root = Path(directories.root)
    if not root.exists() or shutil.which(binary) is None:
        return []
    cleanup_env = env or _cleanup_environment(directories, root / "storage.conf")
    failures: list[str] = []
    containers = _buildah_ids(
        binary,
        directories,
        driver=driver,
        env=cleanup_env,
        kind="containers",
        failures=failures,
    )
    for container_id in containers:
        _run_cleanup(
            binary,
            directories,
            ["umount", container_id],
            driver=driver,
            env=cleanup_env,
            failures=failures,
        )
        _run_cleanup(
            binary,
            directories,
            ["rm", container_id],
            driver=driver,
            env=cleanup_env,
            failures=failures,
        )
    images = _buildah_ids(
        binary,
        directories,
        driver=driver,
        env=cleanup_env,
        kind="images",
        failures=failures,
    )
    for image_id in images:
        _run_cleanup(
            binary,
            directories,
            ["rmi", "--force", image_id],
            driver=driver,
            env=cleanup_env,
            failures=failures,
        )
    return failures


def allocated_path_bytes(path: Path) -> int:
    try:
        root_stat = path.lstat()
    except FileNotFoundError:
        return 0
    total = root_stat.st_blocks * 512
    if not path.is_dir() or path.is_symlink():
        return total
    for child in path.rglob("*"):
        try:
            stat = child.lstat()
        except FileNotFoundError:
            continue
        total += stat.st_blocks * 512
    return total


class _LockedFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: IO[str] | None = None

    def __enter__(self) -> IO[str]:
        self.file = self.path.open("a+", encoding="ascii")
        fcntl.flock(self.file.fileno(), fcntl.LOCK_EX)
        return self.file

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self.file is None:
            return
        fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


def _reserved_bytes(root: Path) -> int:
    total = 0
    for reservation in root.glob(f"{IMAGE_BUILD_SCRATCH_PREFIX}*/.reservation"):
        try:
            total += int(reservation.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            continue
    return total


def _build_root_name(*, worker_id: str, build_id: str, container_id: str) -> str:
    owner = _safe_component(worker_id) or "worker"
    build = _safe_component(build_id) or "build"
    container = _safe_component(container_id) or "container"
    return f"{IMAGE_BUILD_SCRATCH_PREFIX}{owner}-{build}-{container}-{uuid4().hex}"


def _safe_component(value: str) -> str:
    sanitized = "".join(
        character if character.isalnum() or character in "_.-" else "-" for character in value
    )
    return sanitized[:64].strip("-.")


def _stored_driver(root: Path) -> BuildahStorageDriver | None:
    storage_conf = root / "storage.conf"
    if not storage_conf.is_file():
        return None
    try:
        document = _BuildahStorageDocument.model_validate(
            tomllib.loads(storage_conf.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return None
    return document.storage.driver


def _cleanup_environment(directories: BuildahDirectoryPlan, storage_conf: Path) -> dict[str, str]:
    return buildah_environment(
        runroot=directories.runroot,
        tmpdir=directories.tmpdir,
        storage_conf_path=str(storage_conf),
        cpu_count=os.cpu_count() or 1,
        base_env=os.environ,
    )


def _buildah_ids(
    binary: str,
    directories: BuildahDirectoryPlan,
    *,
    driver: BuildahStorageDriver,
    env: dict[str, str],
    kind: str,
    failures: list[str],
) -> list[str]:
    command = _buildah_command(binary, directories, [kind, "--quiet"], driver=driver)
    try:
        process = subprocess.run(
            command,
            cwd=directories.root,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        failures.append(f"{kind}: {type(exc).__name__}: {exc}")
        return []
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()
        failures.append(f"{kind}: buildah exited {process.returncode}: {detail}")
        return []
    return list(dict.fromkeys(line.strip() for line in process.stdout.splitlines() if line.strip()))


def _run_cleanup(
    binary: str,
    directories: BuildahDirectoryPlan,
    args: list[str],
    *,
    driver: BuildahStorageDriver,
    env: dict[str, str],
    failures: list[str],
) -> None:
    try:
        process = subprocess.run(
            _buildah_command(binary, directories, args, driver=driver),
            cwd=directories.root,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        failures.append(f"{' '.join(args)}: {type(exc).__name__}: {exc}")
        return
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()
        failures.append(f"{' '.join(args)}: buildah exited {process.returncode}: {detail}")


def _buildah_command(
    binary: str,
    directories: BuildahDirectoryPlan,
    args: list[str],
    *,
    driver: BuildahStorageDriver,
) -> list[str]:
    return [
        binary,
        "--root",
        directories.graphroot,
        "--runroot",
        directories.runroot,
        "--storage-driver",
        driver.value,
        *args,
    ]


__all__ = [
    "DEFAULT_IMAGE_BUILD_MIN_FREE_BYTES",
    "DEFAULT_IMAGE_BUILD_PER_BUILD_MAX_BYTES",
    "DEFAULT_IMAGE_BUILD_ROOT",
    "DEFAULT_IMAGE_BUILD_SCRATCH_MAX_BYTES",
    "DEFAULT_IMAGE_BUILD_STALE_SECONDS",
    "IMAGE_BUILD_SCRATCH_MONITOR_SECONDS",
    "ImageBuildScratchCapacityError",
    "ImageBuildScratchLease",
    "ImageBuildScratchManager",
    "ImageBuildScratchReconcileResult",
    "allocated_path_bytes",
    "cleanup_isolated_buildah_store",
]
