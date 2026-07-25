from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path


class CredentialFileError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CredentialFilePublication:
    """Crash-safe publication transport for an opaque credential.

    PostgreSQL remains the credential and idempotency authority. The staged
    file exists only so a retry can publish the exact raw token whose hash was
    committed in the same workflow.
    """

    output: Path
    publication_id: str

    @property
    def resolved_output(self) -> Path:
        # Make relative paths deterministic without following the final path.
        # `_read_credential` and the no-follow create/link operations must see
        # and reject an attacker-controlled symlink at the requested output.
        return Path(os.path.abspath(os.fspath(self.output.expanduser())))

    @property
    def staged_path(self) -> Path:
        output = self.resolved_output
        digest = hashlib.sha256(self.publication_id.encode("utf-8")).hexdigest()[:20]
        return output.with_name(f".{output.name}.{digest}.pending")

    def read_published(self) -> str | None:
        return _read_credential(self.resolved_output)

    def read_staged(self) -> str | None:
        return _read_credential(self.staged_path)

    def stage(self, token: str) -> None:
        if not token or "\n" in token or "\r" in token:
            raise CredentialFileError("credential is empty or contains a line break")
        output = self.resolved_output
        output.parent.mkdir(parents=True, exist_ok=True)
        staged = self.staged_path
        existing = _read_credential(staged)
        if existing is not None:
            if existing == token:
                _secure_mode(staged)
                return
            raise CredentialFileError("a different staged credential already exists")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(staged, flags, 0o600)
        try:
            payload = f"{token}\n".encode()
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fsync(descriptor)
        except BaseException:
            try:
                staged.unlink(missing_ok=True)
            finally:
                os.close(descriptor)
            raise
        os.close(descriptor)
        _fsync_directory(output.parent)

    def publish(self, *, replace: bool) -> None:
        staged = self.staged_path
        if _read_credential(staged) is None:
            raise CredentialFileError("staged credential is missing")
        output = self.resolved_output
        if replace:
            os.replace(staged, output)
        else:
            try:
                os.link(staged, output, follow_symlinks=False)
            except FileExistsError as exc:
                raise CredentialFileError(f"credential output already exists: {output}") from exc
            staged.unlink()
        _secure_mode(output)
        _fsync_directory(output.parent)

    def secure_published_mode(self) -> None:
        if self.read_published() is None:
            raise CredentialFileError("published credential is missing")
        _secure_mode(self.resolved_output)
        _fsync_directory(self.resolved_output.parent)

    def discard_staged(self) -> None:
        staged = self.staged_path
        try:
            metadata = staged.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CredentialFileError(f"refusing to remove non-regular staged path: {staged}")
        staged.unlink()
        _fsync_directory(staged.parent)


def _read_credential(path: Path) -> str | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise CredentialFileError(f"credential path is not a regular file: {path}")
    if metadata.st_size > 4096:
        raise CredentialFileError(f"credential file is unexpectedly large: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise CredentialFileError(f"credential file does not contain one token: {path}")
    return value


def _secure_mode(path: Path) -> None:
    os.chmod(path, 0o600, follow_symlinks=False)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["CredentialFileError", "CredentialFilePublication"]
