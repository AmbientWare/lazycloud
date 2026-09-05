from __future__ import annotations

from collections.abc import Generator, Mapping

from lazycloud.clients.image.control import ImageControlClient
from pydantic import JsonValue
from shared.http.errors import HttpTransportError
from shared.http.images import BuildImageEvent, BuildImageRequest, BuildImageResponse
from shared.image_building.records import BuildStatus, ImageBuildPhase


class _ReconnectingImageChannel:
    submission_closed = False
    stream_count = 0

    def post(self, path: str, payload: Mapping[str, JsonValue] | None = None) -> JsonValue:
        raise AssertionError("unexpected non-streaming request")

    def stream_post(
        self, path: str, payload: Mapping[str, JsonValue] | None = None
    ) -> Generator[JsonValue]:
        try:
            yield BuildImageResponse(build_id="build-1").model_dump(mode="json")
            raise AssertionError("submission must close once the build is identified")
        finally:
            self.submission_closed = True

    def stream_get(self, path: str) -> Generator[str]:
        assert self.submission_closed
        self.stream_count += 1
        yield BuildImageEvent(
            sequence=1,
            response=BuildImageResponse(build_id="build-1", msg="building"),
        ).model_dump_json()
        if self.stream_count == 1:
            raise HttpTransportError(
                "GET", "https://api.test/image-builds/build-1/events", "disconnected"
            )
        yield BuildImageEvent(
            sequence=2,
            response=BuildImageResponse(build_id="build-1", msg="building"),
        ).model_dump_json()
        yield BuildImageEvent(
            sequence=3,
            response=BuildImageResponse(
                build_id="build-1",
                image_id="image-1",
                msg="complete",
                done=True,
                success=True,
                status=BuildStatus.Complete,
                phase=ImageBuildPhase.Complete,
            ),
        ).model_dump_json()


def test_image_build_reconnect_preserves_distinct_identical_log_lines() -> None:
    client = ImageControlClient(_ReconnectingImageChannel(), workspace="workspace-1")
    responses = list(client.build_image(BuildImageRequest(python_packages=["httpx"])))

    assert [response.msg for response in responses] == ["building", "building", "complete"]
    assert {response.build_id for response in responses} == {"build-1"}
    assert responses[-1].done and responses[-1].success
