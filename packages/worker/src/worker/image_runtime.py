from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import Field
from shared.contracts import ContractModel

from worker.image_lifecycle import ImageRuntimeConfig
from worker.origin_access import ImageRegistryCredentials


class ImageContentCacheConnection(ContractModel):
    endpoint: str
    token: str = Field(repr=False)


class ImageRuntimeResponse(ContractModel):
    id: str
    ok: bool
    mount_point: str = ""
    image_config: ImageRuntimeConfig | None = None
    mounts: int = Field(default=0, ge=0)
    error: str = ""


class MountedImage(ContractModel):
    mount_point: str
    image_config: ImageRuntimeConfig


@dataclass(slots=True)
class ImageRuntimeClient:
    socket_path: Path = Path("/run/lazycloud/image-runtime.sock")
    timeout_seconds: float = 120.0

    def health(self) -> ImageRuntimeResponse:
        return self._call("health")

    def configure_cache(self, connection: ImageContentCacheConnection) -> None:
        response = self._call("configure-cache", content_cache=connection.model_dump())
        if not response.ok:
            raise RuntimeError(response.error or "image content cache configuration failed")

    def mount(
        self,
        *,
        image_id: str,
        archive_sha256: str,
        archive_path: Path,
        mount_point: Path,
        cache_path: Path,
        storage_image_ref: str,
        credentials: ImageRegistryCredentials,
        preload: bool,
    ) -> MountedImage:
        response = self._call(
            "mount",
            image_id=image_id,
            archive_sha256=archive_sha256,
            archive_path=str(archive_path),
            mount_point=str(mount_point),
            cache_path=str(cache_path),
            storage_image_ref=storage_image_ref,
            preload=preload,
            credentials=credentials.model_dump(mode="json", exclude={"expires_at"}),
        )
        if not response.ok:
            raise RuntimeError(response.error or "image runtime could not mount the image")
        if response.image_config is None or not response.mount_point:
            raise RuntimeError("image mount did not return verified image configuration")
        return MountedImage(mount_point=response.mount_point, image_config=response.image_config)

    def unmount(self, image_id: str) -> None:
        response = self._call("unmount", image_id=image_id)
        if not response.ok:
            raise RuntimeError(response.error or "image runtime could not unmount the image")

    def update_credentials(self, credentials: ImageRegistryCredentials) -> None:
        response = self._call(
            "credentials",
            credentials=credentials.model_dump(mode="json", exclude={"expires_at"}),
        )
        if not response.ok:
            raise RuntimeError(
                response.error or "image runtime could not refresh registry credentials"
            )

    def _call(self, action: str, **payload: object) -> ImageRuntimeResponse:
        request_id = uuid4().hex
        request = json.dumps(
            {"id": request_id, "action": action, **payload},
            separators=(",", ":"),
        ).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout_seconds)
            connection.connect(str(self.socket_path))
            connection.sendall(request + b"\n")
            response = bytearray()
            while not response.endswith(b"\n"):
                chunk = connection.recv(64 * 1024)
                if not chunk:
                    raise RuntimeError("image runtime closed the connection without a response")
                response.extend(chunk)
                if len(response) > 1024 * 1024:
                    raise RuntimeError("image runtime response exceeds one MiB")
        result = ImageRuntimeResponse.model_validate_json(bytes(response))
        if result.id != request_id:
            raise RuntimeError("image runtime returned a mismatched response")
        return result
