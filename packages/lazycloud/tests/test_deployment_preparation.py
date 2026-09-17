from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from lazycloud.session.deployment import DeploymentClient
from lazycloud.session.preparation import DeploymentPreparation
from lazycloud.terminal import ProgressCallback
from shared.deployment_records import DeploymentSpec
from shared.http.images import (
    BuildImageRequest,
    BuildImageResponse,
    VerifyImageBuildRequest,
    VerifyImageBuildResponse,
)
from tests.fakes import FakeDeploymentClient, FakeUploadClient, FakeUploadedObject


@pytest.mark.parametrize("failed_phase", ["image", "source"])
def test_failed_preparation_cannot_create_a_stub_and_a_new_deployment_can_retry(
    tmp_path: Path,
    failed_phase: str,
) -> None:
    (tmp_path / "app.py").write_text("print('hello')\n")
    failing = True

    class Images:
        def verify_image_build(self, request: VerifyImageBuildRequest) -> VerifyImageBuildResponse:
            return VerifyImageBuildResponse(image_id="", valid=True, exists=False)

        def build_image(self, request: BuildImageRequest) -> Iterator[BuildImageResponse]:
            if failing and failed_phase == "image":
                yield BuildImageResponse(done=True, success=False, error="image failed")
            else:
                yield BuildImageResponse(done=True, success=True, image_id="built-image")

    class Uploads(FakeUploadClient):
        def upload_bytes(
            self,
            data: bytes,
            *,
            name: str,
            bucket: str = "default",
            overwrite: bool = False,
            content_type: str = "application/octet-stream",
            metadata: dict[str, str] | None = None,
            progress: ProgressCallback | None = None,
        ) -> FakeUploadedObject:
            if failing and failed_phase == "source":
                raise RuntimeError("source failed")
            return super().upload_bytes(
                data,
                name=name,
                bucket=bucket,
                overwrite=overwrite,
                content_type=content_type,
                metadata=metadata,
                progress=progress,
            )

    gateway = FakeDeploymentClient()
    client = DeploymentClient(
        client=gateway,
        image_client=Images(),
        object_client=Uploads(object_id="source"),
        source_root=tmp_path,
        sync_source=True,
    )
    with DeploymentPreparation() as preparation:
        client.preparation = preparation
        for name in ("first", "second"):
            with pytest.raises(RuntimeError, match=f"{failed_phase} failed"):
                client.prepare(DeploymentSpec(name=name))
        assert gateway.stub_requests == []

    failing = False
    with DeploymentPreparation() as preparation:
        client.preparation = preparation
        response = client.prepare(DeploymentSpec(name="retry"))
    assert response.stub_id == gateway.stub_id
    assert gateway.stub_requests[0].image_id == "built-image"
    assert gateway.stub_requests[0].object_id == "source"
