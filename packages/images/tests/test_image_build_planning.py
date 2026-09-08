from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
from api.server.services import ApiServices
from database.repositories.images import ImageArchiveRepository
from images.control import ImageControlService
from images.publication import (
    ArchiveImageBuildPublicationPublisher,
    ImageBuildPublication,
    ImageBuildPublicationPublishStatus,
    ImageBuildPublicationStatus,
)
from pydantic import JsonValue, TypeAdapter
from shared.http.images import (
    BuildImageRequest,
    BuildImageResponse,
    VerifyImageBuildRequest,
    VerifyImageBuildResponse,
)
from shared.image_building.authoring import ImageSpec
from shared.image_building.records import ImageBuildRecord
from storage.image_archive import ImageArchiveSettings
from storage_client.s3 import S3ObjectInfo

_TEST_BASE_IMAGE_DIGEST = f"sha256:{'a' * 64}"
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def _archive_settings() -> ImageArchiveSettings:
    return ImageArchiveSettings(
        bucket="image-archives",
        presign_seconds=900,
    )


def _image_control_service(services: ApiServices) -> ImageControlService:
    return ImageControlService(
        services,
        base_image_digest_inspector=lambda _source, _credentials: _TEST_BASE_IMAGE_DIGEST,
    )


def _default_workspace_id(services: ApiServices) -> str:
    with services.context.database.session() as session:
        return services.context.default_workspace_id(session)


def _verify_image(
    service: ImageControlService,
    services: ApiServices,
    request: VerifyImageBuildRequest,
) -> VerifyImageBuildResponse:
    return service.verify_image_build(
        request,
        workspace_id=_default_workspace_id(services),
    )


def _build_image(
    service: ImageControlService,
    services: ApiServices,
    request: BuildImageRequest,
) -> Generator[BuildImageResponse, None, None]:
    return service.build_image(
        request,
        workspace_id=_default_workspace_id(services),
    )


def test_image_control_rejects_unresolved_mutable_base(isolated_services: ApiServices) -> None:
    service = ImageControlService(
        isolated_services,
        base_image_digest_inspector=lambda _source, _credentials: "",
    )

    response = _verify_image(
        service,
        isolated_services,
        VerifyImageBuildRequest(existing_image_uri="registry.example/team/app:latest"),
    )

    assert not response.valid
    assert not response.exists
    assert "digest could not be resolved" in response.reason


def test_image_control_rejects_oversized_context_before_download(
    isolated_services: ApiServices,
) -> None:
    downloaded = False

    class ContextReader:
        @dataclass(slots=True)
        class Record:
            size: int

        def get_by_id_for_workspace(
            self,
            object_id: str,
            *,
            workspace_id: str,
        ) -> Record:
            assert workspace_id == _default_workspace_id(isolated_services)
            assert object_id == "oversized-context"
            return self.Record(size=257 * 1024 * 1024)

        def download_by_id_for_workspace(
            self,
            object_id: str,
            target: str | Path,
            *,
            workspace_id: str,
        ) -> Record:
            assert workspace_id == _default_workspace_id(isolated_services)
            nonlocal downloaded
            downloaded = True
            return self.get_by_id_for_workspace(object_id, workspace_id=workspace_id)

    service = ImageControlService(
        isolated_services,
        build_context_reader=ContextReader(),
    )

    response = _verify_image(
        service, isolated_services, VerifyImageBuildRequest(build_ctx_object="oversized-context")
    )

    assert not response.valid
    assert "exceeds maximum size" in response.reason
    assert not downloaded


@pytest.mark.parametrize(
    ("head_size", "head_sha256"),
    [
        (1025, "a" * 64),
        (1024, "b" * 64),
    ],
)
def test_archive_publication_rejects_head_integrity_mismatch(
    isolated_services: ApiServices,
    head_size: int,
    head_sha256: str,
) -> None:
    workspace_id = _default_workspace_id(isolated_services)
    image_id = "image-integrity"
    build_id = "build-integrity"
    archive_sha256 = "a" * 64
    archive_key = f"image-archives/{image_id}.rclip"
    with isolated_services.context.database.session() as session:
        archive, _ = ImageArchiveRepository(session).reserve(
            image_id,
            bucket="image-archives",
            object_key=archive_key,
            size_bytes=1024,
            sha256=archive_sha256,
            registry_ref=f"registry.example.com/workloads@sha256:{'c' * 64}",
            manifest_digest="sha256:" + "c" * 64,
            architecture="amd64",
            format_version=2,
        )

    class IntegrityMismatchStore:
        def exists(self, key: str, *, bucket: str | None = None) -> bool:
            return True

        def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo:
            return S3ObjectInfo(
                bucket=bucket or "image-archives",
                key=key,
                size=head_size,
                metadata={"artifact-sha256": head_sha256},
            )

    result = ArchiveImageBuildPublicationPublisher(
        object_store=IntegrityMismatchStore(),
        settings=_archive_settings(),
        context=isolated_services.context,
    ).publish(
        ImageBuildRecord(
            id=build_id,
            image=ImageSpec(ignore_python=True, commands=["true"]),
            fingerprint="integrity",
            image_id=image_id,
        ),
        ImageBuildPublication(
            status=ImageBuildPublicationStatus.Published,
            workspace_id=workspace_id,
            cache_metadata={
                "published_archive_object_key": archive.object_key,
                "published_archive_size_bytes": str(archive.size_bytes),
                "published_archive_sha256": archive.sha256,
            },
        ),
    )

    assert result.status is ImageBuildPublicationPublishStatus.Error
    assert result.reason == "image build archive failed size or sha256 verification"


def test_image_control_secret_version_invalidates_identity_and_inline_values_fail(
    isolated_services: ApiServices,
) -> None:
    service = _image_control_service(isolated_services)
    isolated_services.secrets.set("API_TOKEN", "first")
    request = VerifyImageBuildRequest(secrets=["API_TOKEN"])
    first = _verify_image(service, isolated_services, request)

    isolated_services.secrets.set("API_TOKEN", "second")
    second = _verify_image(service, isolated_services, request)
    inline = _verify_image(
        service,
        isolated_services,
        VerifyImageBuildRequest(secrets=["API_TOKEN=plaintext"]),
    )

    assert first.valid
    assert second.valid
    assert first.cache_key != second.cache_key
    assert first.image_id != second.image_id
    assert not inline.valid
    assert "stored secret names" in inline.reason


def _json_object(value: JsonValue, *, name: str) -> dict[str, JsonValue]:
    assert isinstance(value, dict), f"{name} must be a JSON object"
    return value
