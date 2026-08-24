from __future__ import annotations

import json
import os
import posixpath
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from worker.tools import WorkspaceStorageCredentials

DEFAULT_WORKSPACE_CREDENTIAL_ROOT = "/var/lib/lazycloud/workspace-credentials"

_CREDENTIAL_PROCESS_READER = "/bin/cat"
"""What the mount runs to read the credential file.

The AWS SDK re-invokes this whenever the cached credential nears its expiry, which
is what refreshes a mount that outlives the container that started it. `cat` is
enough because the file is already the answer: the worker keeps it current, and a
helper that fetched credentials itself would be a second client with its own
token, its own failure mode, and no way to report either to the worker.
"""

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600


def _expiration_text(value: datetime) -> str:
    """RFC 3339, which is the only spelling the reader accepts.

    The Go SDK behind the mount parses this with `Z07:00`, so a UTC offset
    written as `+0000` is rejected and the mount falls back to no credentials at
    all. It reports the parse failure and then times out waiting to mount, which
    names neither the field nor the format.
    """
    moment = value.astimezone(UTC).replace(microsecond=0)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class WorkspaceCredentialFiles:
    """The two files a mount reads its credentials through.

    Kept per workspace and outside the geesefs cache directory: the cache is
    rebuilt freely and holds no secret, and a credential written among it would be
    removed by anything that reclaimed disk.
    """

    root: str
    workspace_name: str

    @property
    def directory(self) -> str:
        return posixpath.join(self.root, self.workspace_name)

    @property
    def shared_config_path(self) -> str:
        return posixpath.join(self.directory, "config")

    @property
    def credentials_path(self) -> str:
        return posixpath.join(self.directory, "credentials.json")

    def write(self, credentials: WorkspaceStorageCredentials) -> None:
        """Publish a credential for the mount to pick up.

        The credential file is replaced atomically because a mount may read it at
        any moment, including part-way through a refresh; a partial read is an
        unparseable credential and a mount that fails for the length of a write.
        """
        directory = Path(self.directory)
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(_DIRECTORY_MODE)

        payload: dict[str, str | int] = {
            "Version": 1,
            "AccessKeyId": credentials.access_key,
            "SecretAccessKey": credentials.secret_key,
        }
        if credentials.session_token:
            payload["SessionToken"] = credentials.session_token
        if credentials.expires_at is not None:
            payload["Expiration"] = _expiration_text(credentials.expires_at)
        self._replace(self.credentials_path, json.dumps(payload))

        config = [
            "[profile workspace]",
            f"credential_process = {_CREDENTIAL_PROCESS_READER} {self.credentials_path}",
        ]
        if credentials.region:
            config.append(f"region = {credentials.region}")
        self._replace(self.shared_config_path, "\n".join(config) + "\n")

    def remove(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)

    def _replace(self, path: str, content: str) -> None:
        handle, staged = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(content)
            os.chmod(staged, _FILE_MODE)
            os.replace(staged, path)
        except BaseException:
            Path(staged).unlink(missing_ok=True)
            raise


__all__ = [
    "DEFAULT_WORKSPACE_CREDENTIAL_ROOT",
    "WorkspaceCredentialFiles",
]
