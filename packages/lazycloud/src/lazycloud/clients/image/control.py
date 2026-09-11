from __future__ import annotations

from collections.abc import Generator, Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue
from shared.http.images import (
    BuildImageEvent,
    BuildImageRequest,
    BuildImageResponse,
    VerifyImageBuildRequest,
    VerifyImageBuildResponse,
)
from shared.http_transport import HttpChannel
from shared.image_building.records import ImageBuildPhase
from shared.transport_retry import TRANSIENT_TRANSPORT_ERRORS, TransientRetry

from lazycloud.control import workspace_path


class ImageControlChannel(Protocol):
    def post(
        self,
        path: str,
        payload: Mapping[str, JsonValue] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> JsonValue: ...

    def stream_post(
        self,
        path: str,
        payload: Mapping[str, JsonValue] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Generator[JsonValue]: ...

    def stream_get(self, path: str, *, timeout_seconds: float | None = None) -> Generator[str]: ...


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
    timeout_seconds: float | None = None

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
                self._scoped("/api/v1/images/verify-build"),
                request.model_dump(mode="json"),
                timeout_seconds=self.timeout_seconds,
            )
        )

    def build_image(self, request: BuildImageRequest) -> Iterator[BuildImageResponse]:
        retry = TransientRetry()
        payload = request.model_dump(mode="json")
        build_id = ""
        cursor = 0
        while True:
            try:
                if not build_id:
                    with closing(
                        self.channel.stream_post(
                            self._scoped("/api/v1/images/build"),
                            payload,
                            timeout_seconds=self.timeout_seconds,
                        )
                    ) as submission:
                        for item in submission:
                            response = BuildImageResponse.model_validate(item)
                            retry.reset()
                            if response.done:
                                yield response
                                return
                            if response.build_id and response.phase is not ImageBuildPhase.Reused:
                                build_id = response.build_id
                                break
                            yield response
                    if not build_id:
                        raise ConnectionError(
                            "image build submission closed without build identity"
                        )
                with closing(
                    self.channel.stream_get(
                        self._scoped(f"/api/v1/image-builds/{build_id}/events?after={cursor}"),
                        timeout_seconds=self.timeout_seconds,
                    )
                ) as events:
                    for line in events:
                        if not line.strip():
                            continue
                        event = BuildImageEvent.model_validate_json(line)
                        response = event.response
                        if response.build_id != build_id:
                            raise ValueError("image build stream changed build identity")
                        if event.sequence > cursor or event.sequence == 0:
                            cursor = max(cursor, event.sequence)
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
