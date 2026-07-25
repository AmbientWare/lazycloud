from __future__ import annotations

from typing import ClassVar

from api.server.services import ApiServices
from images.building import plan_image_build_log_event
from images.execution import ImageBuildExecutionRequest, ImageBuildExecutionResult
from images.service import (
    MAX_IMAGE_BUILD_DIAGNOSTIC_BYTES,
    MAX_IMAGE_BUILD_DIAGNOSTIC_LINE_BYTES,
    MAX_IMAGE_BUILD_DIAGNOSTIC_LINES,
)
from shared.image_building.authoring import ImageSpec
from shared.image_building.records import BuildStatus


def test_failed_image_build_diagnostics_are_useful_bounded_and_sanitized(
    isolated_services: ApiServices,
) -> None:
    secret = "private-build-argument"
    isolated_services.images.executor = _FailingDiagnosticExecutor(secret)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)

    execution = isolated_services.images.execute(
        ImageSpec(base="scratch", ignore_python=True),
        workspace_id=workspace_id,
        build_args={"PRIVATE_TOKEN": secret},
    )
    record = isolated_services.images.get_for_workspace(
        execution.record.id,
        workspace_id=workspace_id,
    )
    replay = isolated_services.images.stream_events(
        record.id,
        workspace_id=workspace_id,
    )

    assert record.status is BuildStatus.Failed
    assert record.error is not None
    assert "executor error 299" in record.error
    assert secret not in record.model_dump_json()
    assert secret not in "".join(event.model_dump_json() for event in replay)
    assert len(record.logs) <= MAX_IMAGE_BUILD_DIAGNOSTIC_LINES
    assert sum(len(item.encode("utf-8")) for item in record.logs) <= (
        MAX_IMAGE_BUILD_DIAGNOSTIC_BYTES
    )
    assert all(
        len(item.encode("utf-8")) <= MAX_IMAGE_BUILD_DIAGNOSTIC_LINE_BYTES for item in record.logs
    )
    assert replay[-1].done
    assert replay[-1].error == record.error


class _FailingDiagnosticExecutor:
    cache_markers: ClassVar[frozenset[str]] = frozenset({"diagnostic-test"})
    requires_archive_publication: ClassVar[bool] = False

    def __init__(self, secret: str) -> None:
        self.secret = secret

    def execute(self, request: ImageBuildExecutionRequest) -> ImageBuildExecutionResult:
        events = [
            plan_image_build_log_event(
                f"executor error {index}: {self.secret} {'x' * 1024}",
                image_id=request.plan.image_id,
                build_id=request.build_id,
                python_version=request.plan.spec.python_version,
            )
            for index in range(300)
        ]
        return ImageBuildExecutionResult(
            status=BuildStatus.Failed,
            events=events,
            reason="v2 build container exited with failure",
        )
