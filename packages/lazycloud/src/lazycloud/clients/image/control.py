from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from shared.http.images import (
    BuildImageRequest,
    BuildImageResponse,
    VerifyImageBuildRequest,
    VerifyImageBuildResponse,
)
from shared.http_transport import HttpChannel
from shared.transport_retry import TRANSIENT_TRANSPORT_ERRORS, TransientRetry

from lazycloud.control import workspace_path


class ImageControlChannel(Protocol):
    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...

    def stream_post(self, path: str, payload: dict[str, Any] | None = None) -> Iterator[Any]: ...


@dataclass
class ImageControlClient:
    channel: ImageControlChannel
    workspace: str = "default"
    """Workspace every call acts in, named rather than inferred.

    A user credential reaches every workspace its owner belongs to, so the request
    has to say which one; inside a container the workspace comes from the environment
    the runner pins. Either way the caller states it rather than letting the server
    pick one.
    """

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        workspace: str = "default",
        timeout_seconds: float = 10.0,
    ) -> ImageControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def _scoped(self, path: str) -> str:
        return workspace_path(path, self.workspace)

    def verify_image_build(
        self,
        request: VerifyImageBuildRequest,
    ) -> VerifyImageBuildResponse:
        return VerifyImageBuildResponse.model_validate(
            self.channel.post(
                self._scoped("/api/v1/images/verify-build"), request.model_dump(mode="json")
            )
        )

    def build_image(self, request: BuildImageRequest) -> Iterator[BuildImageResponse]:
        retry = TransientRetry()
        payload = request.model_dump(mode="json")
        seen: set[tuple[str, str, str, bool, bool, str, bool, str, str, str]] = set()
        while True:
            try:
                for item in self.channel.stream_post(
                    self._scoped("/api/v1/images/build"),
                    payload,
                ):
                    response = BuildImageResponse.model_validate(item)
                    key = (
                        response.image_id,
                        response.build_id,
                        response.msg,
                        response.done,
                        response.success,
                        response.python_version,
                        response.warning,
                        response.status.value,
                        response.phase.value,
                        response.error,
                    )
                    if key not in seen:
                        seen.add(key)
                        retry.reset()
                        yield response
                    if response.done:
                        return
            except TRANSIENT_TRANSPORT_ERRORS as exc:
                retry.backoff(exc)
            else:
                retry.backoff(ConnectionError("image build stream closed by control plane"))


__all__ = [
    "ImageControlChannel",
    "ImageControlClient",
]
