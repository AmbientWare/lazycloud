from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr

NEWT_CONFIG_FILE = "newt.json"
NEWT_CONNECTION_FILE = "connection.json"
NEWT_HEALTH_FILE = "healthy"
NEWT_LOG_FILE = "newt.log"


class NewtConnection(BaseModel):
    model_config = ConfigDict(frozen=True)

    site_name: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    connector_id: str = Field(min_length=1)
    secret: SecretStr


class NewtSite(BaseModel):
    model_config = ConfigDict(frozen=True)

    site_name: str = Field(alias="siteName", min_length=1)
    connector_id: str = Field(default="", alias="connectorId")


@dataclass(frozen=True, slots=True)
class NewtRuntimeStatus:
    running: bool
    healthy: bool
    exit_code: int | None
    connection: NewtSite


@dataclass(slots=True)
class NewtRuntime:
    state_dir: Path
    binary: str = "newt"
    _process: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _log_handle: BinaryIO | None = field(default=None, init=False, repr=False)

    @property
    def config_path(self) -> Path:
        return self.state_dir / NEWT_CONFIG_FILE

    @property
    def connection_path(self) -> Path:
        return self.state_dir / NEWT_CONNECTION_FILE

    @property
    def health_path(self) -> Path:
        return self.state_dir / NEWT_HEALTH_FILE

    @property
    def log_path(self) -> Path:
        return self.state_dir / NEWT_LOG_FILE

    @property
    def configured(self) -> bool:
        return self.config_path.is_file() and self.connection_path.is_file()

    def configure(self, connection: NewtConnection) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        config: dict[str, JsonValue] = {
            "endpoint": connection.endpoint,
            "name": connection.site_name,
            "id": connection.connector_id,
            "secret": connection.secret.get_secret_value(),
            "disableSsh": True,
            "healthFile": str(self.health_path),
            "logLevel": "INFO",
        }
        metadata: dict[str, JsonValue] = {
            "siteName": connection.site_name,
        }
        _write_json_atomic(self.config_path, config, permissions=0o600)
        _write_json_atomic(self.connection_path, metadata, permissions=0o600)

    def start(self, connection: NewtConnection | None = None) -> NewtRuntimeStatus:
        if self._process is not None and self._process.poll() is None:
            return self.status()
        if connection is not None:
            self.configure(connection)
        saved = self._load_connection()
        self.health_path.unlink(missing_ok=True)
        self._close_log()
        self._log_handle = self.log_path.open("ab", buffering=0)
        self._process = subprocess.Popen(
            [self.binary, "--config-file", str(self.config_path)],
            cwd=self.state_dir,
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
        return NewtRuntimeStatus(
            running=True,
            healthy=False,
            exit_code=None,
            connection=saved,
        )

    def status(self) -> NewtRuntimeStatus:
        connection = self._load_connection()
        process = self._process
        exit_code = None if process is None else process.poll()
        return NewtRuntimeStatus(
            running=process is not None and exit_code is None,
            healthy=self.health_path.is_file() and exit_code is None,
            exit_code=exit_code,
            connection=connection,
        )

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.health_path.unlink(missing_ok=True)
        self._close_log()

    def discard(self) -> None:
        self.close()
        self.config_path.unlink(missing_ok=True)
        self.connection_path.unlink(missing_ok=True)

    def _load_connection(self) -> NewtSite:
        if not self.config_path.is_file() or not self.connection_path.is_file():
            raise RuntimeError("Newt has no saved Pangolin site configuration")
        metadata = NewtSite.model_validate_json(self.connection_path.read_text(encoding="utf-8"))
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        connector_id = config.get("id", "")
        if not isinstance(connector_id, str):
            connector_id = ""
        return metadata.model_copy(update={"connector_id": connector_id})

    def _close_log(self) -> None:
        handle = self._log_handle
        self._log_handle = None
        if handle is not None:
            handle.close()


def _write_json_atomic(path: Path, payload: dict[str, JsonValue], *, permissions: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.chmod(permissions)
    os.replace(temporary, path)


__all__ = [
    "NewtConnection",
    "NewtRuntime",
    "NewtRuntimeStatus",
    "NewtSite",
]
