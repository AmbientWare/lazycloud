"""Image resolution and builds through the API, shown as the terminal step Image.

Kept apart from `Image` so declaring an app loads no API client.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING, TypeVar
from uuid import UUID

from typing_extensions import Self

from lazycloud.abstractions.image import Image, ImageBuildResult, ImageVerification
from lazycloud.clients.api import ApiClient, ApiError, is_transient
from lazycloud.contracts.api import (
    ErrorCode,
    ImageBuild,
    ImageBuildLogEntry,
    ImageBuildPhase,
    ImageBuildStatus,
    ImageDefinition,
)
from lazycloud.control import api_client, require_workspace, resolve_control_client_config
from lazycloud.exceptions import UnsupportedFeatureError
from lazycloud.session.task import retry_backoff, retry_budget_spent
from lazycloud.terminal import Terminal, TerminalStep

if TYPE_CHECKING:
    from collections.abc import Mapping

T = TypeVar("T")

# Most a build read holds before answering; the API allows 60.
_BUILD_WAIT_SECONDS = 30
_MAX_BUILD_ATTEMPTS = 2
# Rejections of the definition itself, reported as an invalid image.
_DEFINITION_ERRORS = frozenset({ErrorCode.invalid_request, ErrorCode.unsupported})
_PHASE_SUMMARIES = {
    ImageBuildPhase.queued: "queued",
    ImageBuildPhase.starting: "starting build container",
    ImageBuildPhase.building: "building",
    ImageBuildPhase.finished: "finishing",
}
_WAITING_SUMMARIES = frozenset(
    {_PHASE_SUMMARIES[ImageBuildPhase.queued], _PHASE_SUMMARIES[ImageBuildPhase.starting]}
)


@dataclass(slots=True)
class ImageBuildOperation:
    """One image's preparation, shown as the terminal step Image."""

    image: Image
    client: ApiClient
    workspace: str
    terminal: Terminal | None = None
    env: Mapping[str, str] | None = field(default=None, repr=False)
    machine: str = ""
    _step: TerminalStep | None = field(default=None, init=False, repr=False)
    _definition: ImageDefinition | None = field(default=None, init=False, repr=False)
    _result: ImageBuildResult | None = field(default=None, init=False)
    _stage: str = field(default="", init=False)
    _attempt: int = field(default=1, init=False)

    def __enter__(self) -> Self:
        terminal = self.terminal or Terminal(quiet=True)
        self._step = terminal.step("Image", "preparing")
        self._step.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._step is not None:
            self._step.__exit__(exc_type, exc, traceback)

    @property
    def step(self) -> TerminalStep:
        if self._step is None:
            raise RuntimeError("an image build operation runs inside a with block")
        return self._step

    def verify(self) -> None:
        """Finish at once when the image is ready, rejected or an unknown id."""
        if self.machine:
            raise UnsupportedFeatureError("image build", ["machine"])
        image = self.image
        if image.explicit_image_id:
            verification = verify_id(self.client, self.workspace, image.explicit_image_id)
        else:
            self._definition = image._prepared_definition(self.client, self.workspace, self.env)
            verification = verify_definition(self.client, self.workspace, self._definition)
        if verification.exists:
            self._result = verification_result(image, verification)
            self.step.done(f"python {verification.python_version} · cached")
        elif not verification.valid or image.explicit_image_id:
            self._result = verification_result(image, verification)
            self.step.fail(self._result.error)

    def finish(self) -> ImageBuildResult:
        if self._result is not None:
            return self._result
        if self._definition is None:
            raise RuntimeError("image build requires completed verification")
        try:
            resolution = self.client.build_image(self.workspace, self._definition)
        except ApiError as exc:
            if exc.code not in _DEFINITION_ERRORS:
                raise
            return self._failed(exc.message)
        image = resolution.image
        if resolution.build is None:
            if not image.ready:
                return self._failed(f"the API started no build for image {image.id}")
            self.step.done(f"python {image.python_version} · cached")
            return ImageBuildResult(
                success=True, image_id=image.id, python_version=image.python_version
            )
        build = self._follow(resolution.build)
        if build.status is ImageBuildStatus.succeeded:
            self.step.done(f"python {image.python_version} · built")
            return ImageBuildResult(
                success=True,
                image_id=image.id,
                python_version=image.python_version,
                build_id=str(build.id),
            )
        return self._failed(
            build.failure or "image build failed",
            image_id=image.id,
            python_version=image.python_version,
            build_id=str(build.id),
        )

    def _failed(
        self, error: str, *, image_id: str = "", python_version: str = "", build_id: str = ""
    ) -> ImageBuildResult:
        self.step.fail(error)
        return ImageBuildResult(
            success=False,
            image_id=image_id,
            python_version=python_version,
            build_id=build_id,
            error=error,
        )

    def _follow(self, build: ImageBuild) -> ImageBuild:
        """Show the build's output until it finishes, resuming dropped streams."""
        self._show_build(build)
        build_id = build.id
        cursor = 0
        while True:
            cursor = self._follow_logs(build_id, cursor)
            build = _retry_transient(
                lambda: self.client.get_image_build(
                    self.workspace, build_id, wait_seconds=_BUILD_WAIT_SECONDS
                )
            )
            if build.status is not ImageBuildStatus.building:
                return build
            self._show_build(build)

    def _follow_logs(self, build_id: UUID, cursor: int) -> int:
        failures = 0
        failing_since = 0.0
        while True:
            try:
                for entry in self.client.stream_image_build_logs(
                    self.workspace, build_id, after=cursor, follow=True
                ):
                    cursor = entry.id
                    failures = 0
                    self._show_log(entry)
                return cursor
            except Exception as exc:
                failures += 1
                if failures == 1:
                    failing_since = time.monotonic()
                if not is_transient(exc) or retry_budget_spent(failing_since):
                    raise
                retry_backoff(failures)

    def _show_build(self, build: ImageBuild) -> None:
        # A running build keeps the step it last showed.
        if (
            not self._stage
            or build.attempt != self._attempt
            or build.phase is not ImageBuildPhase.building
        ):
            self._stage = _PHASE_SUMMARIES[build.phase]
        self._attempt = build.attempt
        self.step.update(_attempt_summary(self._stage, self._attempt))

    def _show_log(self, entry: ImageBuildLogEntry) -> None:
        self.step.log(entry.data)
        # Output only comes from a running build container.
        if entry.attempt != self._attempt or self._stage in _WAITING_SUMMARIES:
            self._attempt = entry.attempt
            self._stage = _PHASE_SUMMARIES[ImageBuildPhase.building]
        self._stage = _build_summary(entry.data, self._stage)
        self.step.update(_attempt_summary(self._stage, self._attempt))


def session(client: ApiClient | None, workspace: str | None) -> tuple[ApiClient, str]:
    config = resolve_control_client_config(workspace=workspace)
    return client or api_client(config), require_workspace(config)


def verify_id(client: ApiClient, workspace: str, image_id: str) -> ImageVerification:
    try:
        image = client.get_image(workspace, image_id)
    except ApiError as exc:
        if exc.code is not ErrorCode.not_found and exc.code not in _DEFINITION_ERRORS:
            raise
        return ImageVerification(image_id=image_id, valid=False, exists=False, reason=exc.message)
    return ImageVerification(
        image_id=image.id,
        valid=True,
        exists=image.ready,
        reason="" if image.ready else f"image {image.id} is not ready",
        python_version=image.python_version,
    )


def verify_definition(
    client: ApiClient,
    workspace: str,
    definition: ImageDefinition,
    *,
    force_rebuild: bool = False,
) -> ImageVerification:
    try:
        resolution = client.resolve_image(workspace, definition)
    except ApiError as exc:
        if exc.code not in _DEFINITION_ERRORS:
            raise
        return ImageVerification(image_id="", valid=False, exists=False, reason=exc.message)
    return ImageVerification(
        image_id=resolution.image.id,
        valid=True,
        exists=resolution.image.ready and not force_rebuild,
        build_id=str(resolution.build.id) if resolution.build is not None else "",
        python_version=resolution.image.python_version,
    )


def verification_result(image: Image, verification: ImageVerification) -> ImageBuildResult:
    failed = not verification.valid or (
        image.explicit_image_id is not None and not verification.exists
    )
    return ImageBuildResult(
        success=verification.exists,
        image_id=verification.image_id,
        python_version=verification.python_version,
        build_id=verification.build_id,
        error=verification.reason if failed else "",
    )


def _retry_transient(call: Callable[[], T]) -> T:
    failures = 0
    failing_since = 0.0
    while True:
        try:
            return call()
        except Exception as exc:
            failures += 1
            if failures == 1:
                failing_since = time.monotonic()
            if not is_transient(exc) or retry_budget_spent(failing_since):
                raise
            retry_backoff(failures)


def _attempt_summary(summary: str, attempt: int) -> str:
    if attempt > 1:
        return f"retry {attempt}/{_MAX_BUILD_ATTEMPTS} · {summary}"
    return summary


# BuildKit's plain progress names each Dockerfile step, as in
# `#7 [2/4] RUN pip install numpy` or `#5 [builder 1/3] FROM docker.io/...`.
_BUILDKIT_STEP = re.compile(r"^#\d+ \[(?:\S+ )?(\d+)/(\d+)\] (.*)$")


def _build_summary(line: str, current: str) -> str:
    """The one-line build summary a log line implies, or the current one."""
    matched = _BUILDKIT_STEP.match(line.strip())
    if matched is None:
        return current
    instruction = matched.group(3).split("@", 1)[0]
    return f"step {matched.group(1)}/{matched.group(2)} · {instruction[:48]}"


__all__ = [
    "ImageBuildOperation",
    "session",
    "verification_result",
    "verify_definition",
    "verify_id",
]
