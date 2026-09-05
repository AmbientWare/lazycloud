from __future__ import annotations

from pathlib import Path

import pytest
from images.building import build_image_plan, plan_image_build_session
from images.execution import ImageBuildExecutionRequest
from images.scheduling import plan_image_build_container_request
from shared.image_building.authoring import ImageSpec, LinuxArchitecture


def test_image_architecture_changes_cache_identity_and_scheduler_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    amd64_plan = build_image_plan(
        ImageSpec(architecture=LinuxArchitecture.Amd64, ignore_python=True)
    )
    arm64_plan = build_image_plan(
        ImageSpec(architecture=LinuxArchitecture.Arm64, ignore_python=True)
    )
    assert amd64_plan.cache_key != arm64_plan.cache_key
    assert amd64_plan.image_id != arm64_plan.image_id

    monkeypatch.setattr(
        "images.scheduling.managed_package_source_digest",
        lambda: "a" * 64,
    )
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    request = ImageBuildExecutionRequest(
        build_id="build-1",
        image_id=amd64_plan.image_id,
        tag="local:test",
        build_dir=str(build_dir),
        dockerfile_path=str(build_dir / "Dockerfile"),
        manifest_path=str(build_dir / "manifest.json"),
        plan=amd64_plan,
        session=plan_image_build_session(
            amd64_plan.spec,
            image_id=amd64_plan.image_id,
            build_id="build-1",
        ),
    )

    scheduled = plan_image_build_container_request(request, workspace_id="workspace-1")

    assert scheduled.scheduler_request.architecture == "amd64"
    assert scheduled.build_options.architecture is LinuxArchitecture.Amd64
    payload_build_options = scheduled.scheduler_request.payload["build_options"]
    assert isinstance(payload_build_options, dict)
    assert payload_build_options["architecture"] == "amd64"
