from __future__ import annotations

import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from secrets import token_urlsafe
from tempfile import NamedTemporaryFile
from uuid import UUID

from pydantic import SecretStr

PROVIDER_LAUNCH_ID_FILE = "provider-launch-id"
PROVIDER_BOOTSTRAP_TOKEN_FILE = "provider-bootstrap-token"
PROVIDER_NODE_TOKEN_FILE = "provider-node-token"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43,128}")


@dataclass(frozen=True, slots=True)
class ProviderHostCredentials:
    state_dir: Path

    def _validate_directory(self) -> None:
        info = self.state_dir.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("provider identity directory must belong to root with mode 0700")

    def _read(self, name: str) -> str:
        self._validate_directory()
        path = self.state_dir / name
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("provider identity files must belong to root with mode 0600")
            if not stat.S_ISREG(info.st_mode) or info.st_size > 256:
                raise ValueError("invalid provider identity file")
            return os.read(descriptor, 257).decode("ascii").strip()
        finally:
            os.close(descriptor)

    def launch_id(self) -> str:
        return str(UUID(self._read(PROVIDER_LAUNCH_ID_FILE)))

    def node_token(self) -> SecretStr:
        self._validate_directory()
        if os.geteuid() != 0:
            raise ValueError("provider host enrollment requires root")
        path = self.state_dir / PROVIDER_NODE_TOKEN_FILE
        try:
            return self._token(PROVIDER_NODE_TOKEN_FILE)
        except FileNotFoundError:
            with NamedTemporaryFile(mode="w", encoding="ascii", dir=self.state_dir) as handle:
                handle.write(token_urlsafe(32))
                handle.flush()
                os.fsync(handle.fileno())
                with suppress(FileExistsError):
                    os.link(handle.name, path, follow_symlinks=False)
            directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return self._token(PROVIDER_NODE_TOKEN_FILE)

    def bootstrap_token(self) -> SecretStr:
        try:
            return self._token(PROVIDER_BOOTSTRAP_TOKEN_FILE)
        except FileNotFoundError:
            return SecretStr("")

    def _token(self, name: str) -> SecretStr:
        value = self._read(name)
        if _TOKEN.fullmatch(value) is None:
            raise ValueError("invalid provider identity token file")
        return SecretStr(value)

    def acknowledge(self) -> None:
        self._validate_directory()
        (self.state_dir / PROVIDER_BOOTSTRAP_TOKEN_FILE).unlink(missing_ok=True)
