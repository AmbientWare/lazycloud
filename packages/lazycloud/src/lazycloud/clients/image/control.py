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


class ImageControlChannel(Protocol):
    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...

    def stream_post(self, path: str, payload: dict[str, Any] | None = None) -> Iterator[Any]: ...


@dataclass
class ImageControlClient:
    channel: ImageControlChannel

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> ImageControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds)
        )

    def verify_image_build(
        self,
        request: VerifyImageBuildRequest,
    ) -> VerifyImageBuildResponse:
        return VerifyImageBuildResponse.model_validate(
            self.channel.post("/api/v1/images/verify-build", request.model_dump(mode="json"))
        )

    def build_image(self, request: BuildImageRequest) -> Iterator[BuildImageResponse]:
        for item in self.channel.stream_post(
            "/api/v1/images/build", request.model_dump(mode="json")
        ):
            yield BuildImageResponse.model_validate(item)


__all__ = [
    "ImageControlChannel",
    "ImageControlClient",
]
