from __future__ import annotations

import hashlib
import tempfile
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from shared.env import parse_environment
from shared.errors import InvalidInputError
from shared.http.images import (
    BuildImageEvent,
    BuildImageRequest,
    BuildImageResponse,
    BuildStep,
    VerifyImageBuildRequest,
    VerifyImageBuildResponse,
)
from shared.image_building.authoring import ImageBuildStep, ImageBuildStepKind, ImageSpec
from shared.image_building.credentials import image_secret_names
from shared.image_building.records import ImageBuildRecord, ImageRecord

from images.building import (
    BaseImageDigestCache,
    BaseImageDigestInspector,
    BaseImageDigestRequest,
    ImageBuildCredentialPlan,
    ImageBuildStreamEventPlan,
    ImageRegistryCredentialPayload,
    ImageSourceReference,
    build_image_plan,
    image_build_source_plan,
    marshal_registry_credentials,
    pin_dockerfile_base_images,
    plan_image_build_failure_event,
    plan_image_build_registry_credentials,
    plan_image_build_reused_stream,
    registry_credentials_for_image,
    resolve_base_image_digest,
)
from images.context import ImageSecretReader
from images.metadata import CURRENT_IMAGE_CLIP_VERSION

IMAGE_BUILD_STREAM_POLL_SECONDS = 0.25
MAX_IMAGE_BUILD_CONTEXT_ARCHIVE_BYTES = 256 * 1024 * 1024


class ImageBuildWorkflow(Protocol):
    def get(self, build_id: str, *, workspace_id: str | None = None) -> ImageBuildRecord: ...
    def find_by_request_id(
        self, request_id: str, *, workspace_id: str
    ) -> ImageBuildRecord | None: ...

    def get_image_metadata(
        self,
        image_id: str,
        *,
        workspace_id: str,
    ) -> ImageRecord | None: ...

    def find_reusable_by_image_id(
        self, image_id: str, *, workspace_id: str | None = None
    ) -> ImageBuildRecord | None: ...

    def find_by_image_id(
        self, image_id: str, *, workspace_id: str | None = None
    ) -> ImageBuildRecord | None: ...

    def find_reusable_by_fingerprint(
        self, fingerprint: str, *, workspace_id: str | None = None
    ) -> ImageBuildRecord | None: ...

    def find_by_fingerprint(
        self, fingerprint: str, *, workspace_id: str | None = None
    ) -> ImageBuildRecord | None: ...

    def build(
        self,
        image: ImageSpec,
        *,
        workspace_id: str | None = None,
        tag: str | None = None,
        credential_plan: ImageBuildCredentialPlan | None = None,
        registry_credential_payload: str | None = None,
        build_args: dict[str, str] | None = None,
        request_id: str | None = None,
    ) -> ImageBuildRecord: ...

    def stream_events(
        self, build_id: str, *, workspace_id: str | None = None, after: int = 0
    ) -> list[ImageBuildStreamEventPlan]: ...

    def persist_image_metadata(
        self,
        build_id: str,
        *,
        workspace_id: str | None = None,
        clip_version: int = CURRENT_IMAGE_CLIP_VERSION,
    ) -> ImageRecord: ...

    def cancel(
        self,
        build_id: str,
        *,
        workspace_id: str | None = None,
        reason: str = "Build was aborted.",
    ) -> ImageBuildRecord: ...


class ImageBuildContextObject(Protocol):
    size: int


class ImageBuildContextReader(Protocol):
    def get_by_id_for_workspace(
        self,
        object_id: str,
        *,
        workspace_id: str,
    ) -> ImageBuildContextObject: ...

    def download_by_id_for_workspace(
        self,
        object_id: str,
        target: str | Path,
        *,
        workspace_id: str,
    ) -> ImageBuildContextObject: ...


class ImageRegistryCredentialResolver(Protocol):
    def __call__(
        self,
        payload: ImageRegistryCredentialPayload,
    ) -> ImageRegistryCredentialPayload: ...


class ImageControlDependencies(Protocol):
    @property
    def images(self) -> ImageBuildWorkflow: ...

    @property
    def secrets(self) -> ImageSecretReader: ...


@dataclass(slots=True)
class ImageControlService:
    services: ImageControlDependencies
    base_image_digest_inspector: BaseImageDigestInspector | None = None
    base_image_digests: BaseImageDigestCache = field(default_factory=BaseImageDigestCache)
    build_context_reader: ImageBuildContextReader | None = None
    registry_credential_resolver: ImageRegistryCredentialResolver | None = None

    def verify_image_build(
        self,
        request: VerifyImageBuildRequest,
        *,
        workspace_id: str,
    ) -> VerifyImageBuildResponse:
        try:
            if request.image_id:
                metadata = self.services.images.get_image_metadata(
                    request.image_id,
                    workspace_id=workspace_id,
                )
                reusable = (
                    None
                    if request.force_rebuild
                    else self.services.images.find_reusable_by_image_id(
                        request.image_id,
                        workspace_id=workspace_id,
                    )
                )
                existing = reusable or self.services.images.find_by_image_id(
                    request.image_id,
                    workspace_id=workspace_id,
                )
                cache_key = existing.cache_key if existing and existing.cache_key else ""
                exists = reusable is not None
                return VerifyImageBuildResponse(
                    image_id=request.image_id,
                    valid=True,
                    exists=exists,
                    build_id=reusable.id if reusable else existing.id if existing else "",
                    cache_key=cache_key,
                    reason=_verify_reason(metadata is not None, exists),
                )
            _validate_secret_references(request.secrets)
            context_digest = self._resolve_build_context(
                request.build_ctx_object,
                request.build_ctx_digest,
                workspace_id=workspace_id,
            )
            spec, _build_args = self._resolve_build_secrets(
                _image_spec_from_verify(request, context_digest=context_digest),
                workspace_id=workspace_id,
            )
            resolved_registry_payloads: dict[str, ImageRegistryCredentialPayload] = {}
            spec = self._resolve_image_spec(
                spec,
                credentials=request.existing_image_creds,
                resolved_registry_payloads=resolved_registry_payloads,
            )
            return self._verify_spec(
                spec,
                workspace_id=workspace_id,
                force_rebuild=request.force_rebuild,
            )
        except Exception as exc:
            return VerifyImageBuildResponse(
                image_id=request.image_id or "",
                valid=False,
                exists=False,
                reason=str(exc),
            )

    def follow_build(
        self, build_id: str, *, workspace_id: str, after: int = 0
    ) -> Iterator[BuildImageEvent]:
        self.services.images.get(build_id, workspace_id=workspace_id)
        return stream_build_events(
            self.services.images, build_id, workspace_id=workspace_id, after=after
        )

    def build_image(
        self,
        request: BuildImageRequest,
        *,
        workspace_id: str,
    ) -> Generator[BuildImageResponse]:
        prior = self.services.images.find_by_request_id(
            str(request.request_id), workspace_id=workspace_id
        )
        if prior is not None:
            for event in stream_build_events(
                self.services.images, prior.id, workspace_id=workspace_id
            ):
                yield event.response
            return
        try:
            _validate_secret_references(request.secrets)
            context_digest = self._resolve_build_context(
                request.build_ctx_object,
                request.build_ctx_digest,
                workspace_id=workspace_id,
            )
            spec, build_args = self._resolve_build_secrets(
                _image_spec_from_build(request, context_digest=context_digest),
                workspace_id=workspace_id,
            )
            resolved_registry_payloads: dict[str, ImageRegistryCredentialPayload] = {}
            spec = self._resolve_image_spec(
                spec,
                credentials=request.existing_image_creds,
                resolved_registry_payloads=resolved_registry_payloads,
            )
            verify = self._verify_spec(spec, workspace_id=workspace_id)
        except Exception as exc:
            yield _failed_build_response(exc, python_version=request.python_version)
            return
        if not verify.valid:
            yield _failed_build_response(
                ValueError(verify.reason or "image build verification failed"),
                image_id=verify.image_id,
                python_version=request.python_version,
            )
            return
        try:
            resolved_credential_payload = _resolved_credential_payload_for_spec(
                spec,
                resolved_registry_payloads,
            )
            credential_plan = plan_image_build_registry_credentials(
                source_image=_source_image_for_spec(spec),
                credentials=(
                    resolved_credential_payload.credentials
                    if resolved_credential_payload is not None
                    else {}
                ),
                resolved_payload=resolved_credential_payload,
            )
        except Exception as exc:
            yield _failed_build_response(
                exc,
                image_id=verify.image_id,
                python_version=request.python_version,
            )
            return
        if verify.exists:
            yield from _responses_from_stream_plans(
                plan_image_build_reused_stream(
                    image_id=verify.image_id,
                    build_id=verify.build_id,
                    python_version=request.python_version,
                )
            )
            return
        yield from _stream_image_execution(
            self.services,
            spec,
            request_id=str(request.request_id),
            workspace_id=workspace_id,
            credential_plan=credential_plan,
            registry_credential_payload=_build_source_credential_payload(
                resolved_credential_payload,
                required=credential_plan.use_source_pull_credentials,
            ),
            build_args=build_args,
            python_version=request.python_version,
            image_id=verify.image_id,
        )

    def _verify_spec(
        self,
        spec: ImageSpec,
        *,
        workspace_id: str,
        force_rebuild: bool = False,
    ) -> VerifyImageBuildResponse:
        plan = build_image_plan(spec)
        reusable = (
            None
            if force_rebuild
            else self.services.images.find_reusable_by_fingerprint(
                plan.cache_key,
                workspace_id=workspace_id,
            )
        )

        existing = reusable or self.services.images.find_by_fingerprint(
            plan.cache_key,
            workspace_id=workspace_id,
        )
        exists = reusable is not None
        return VerifyImageBuildResponse(
            image_id=plan.image_id,
            valid=True,
            exists=exists,
            build_id=reusable.id if reusable else existing.id if existing else "",
            cache_key=plan.cache_key,
            reason="image already exists" if exists else "image build required",
        )

    def _resolve_build_secrets(
        self,
        spec: ImageSpec,
        *,
        workspace_id: str,
    ) -> tuple[ImageSpec, dict[str, str]]:
        versions: dict[str, str] = {}
        values: dict[str, str] = {}
        for name in spec.secrets:
            record = self.services.secrets.get(name, workspace=workspace_id)
            versions[name] = record.updated_at.isoformat()
            values[name] = record.value
        return spec.model_copy(update={"build_secret_versions": versions}), values

    def _resolve_build_context(
        self,
        object_id: str,
        claimed_digest: str,
        *,
        workspace_id: str,
    ) -> str | None:
        if claimed_digest and not object_id:
            raise InvalidInputError("build context digest requires an uploaded context object")
        if not object_id:
            return None
        if self.build_context_reader is None:
            raise RuntimeError("build context object reader is not configured")
        record = self.build_context_reader.get_by_id_for_workspace(
            object_id,
            workspace_id=workspace_id,
        )
        object_size = record.size
        if object_size < 0:
            raise InvalidInputError("build context object size metadata is invalid")
        if object_size > MAX_IMAGE_BUILD_CONTEXT_ARCHIVE_BYTES:
            raise InvalidInputError(
                "build context archive exceeds maximum size of "
                f"{MAX_IMAGE_BUILD_CONTEXT_ARCHIVE_BYTES} bytes"
            )
        with tempfile.TemporaryDirectory(prefix="lazycloud-image-context-") as directory:
            context_path = Path(directory) / "context.zip"
            self.build_context_reader.download_by_id_for_workspace(
                object_id,
                context_path,
                workspace_id=workspace_id,
            )
            actual_size = context_path.stat().st_size
            if actual_size != object_size:
                raise InvalidInputError("build context object size does not match stored metadata")
            digest = hashlib.sha256()
            with context_path.open("rb") as source:
                while chunk := source.read(8 * 1024 * 1024):
                    digest.update(chunk)
            actual_digest = digest.hexdigest()
        if claimed_digest and claimed_digest != actual_digest:
            raise InvalidInputError("build context digest does not match uploaded object content")
        return actual_digest

    def _resolve_image_spec(
        self,
        spec: ImageSpec,
        *,
        credentials: dict[str, str],
        resolved_registry_payloads: dict[str, ImageRegistryCredentialPayload],
    ) -> ImageSpec:
        def pin(reference: ImageSourceReference) -> str:
            if reference.registry == "docker.io" and reference.repository == "scratch":
                return "scratch"
            if reference.digest:
                return reference.source_image
            if self.base_image_digest_inspector is None:
                raise RuntimeError("base image registry inspector is not configured")
            payload = resolved_registry_payloads.get(reference.registry)
            if payload is None:
                payload = registry_credentials_for_image(reference.source_image, credentials)
                if payload.has_credentials and self.registry_credential_resolver is not None:
                    payload = self.registry_credential_resolver(payload)
                resolved_registry_payloads[reference.registry] = payload
            resolution = resolve_base_image_digest(
                BaseImageDigestRequest(
                    registry=reference.registry,
                    name=reference.repository,
                    tag=reference.tag or "latest",
                    credentials=(
                        marshal_registry_credentials(payload) if payload.has_credentials else ""
                    ),
                    cacheable=not payload.has_credentials,
                ),
                inspector=self.base_image_digest_inspector,
                cache=self.base_image_digests,
            )
            if not resolution.resolved:
                reason = resolution.reason or resolution.status.value
                raise RuntimeError(
                    "base image digest could not be resolved for "
                    f"{reference.source_image}: {reason}"
                )
            return reference.model_copy(update={"digest": resolution.digest}).source_image

        if spec.dockerfile:
            return spec.model_copy(
                update={"dockerfile": pin_dockerfile_base_images(spec.dockerfile, pin)}
            )
        source = image_build_source_plan(spec)
        if source.reference is None:
            raise InvalidInputError("image base reference could not be parsed")
        return spec.model_copy(update={"base": pin(source.reference)})


def _image_spec_from_verify(
    request: VerifyImageBuildRequest,
    *,
    context_digest: str | None,
) -> ImageSpec:
    return ImageSpec(
        architecture=request.architecture,
        base=request.existing_image_uri or "python:3.12-slim",
        python_version=request.python_version or "3.12",
        packages=list(request.python_packages),
        commands=list(request.commands),
        build_steps=[_build_step(step) for step in request.build_steps],
        env=parse_environment(request.env_vars),
        dockerfile=request.dockerfile or None,
        context_digest=context_digest,
        context_object_id=request.build_ctx_object or None,
        credential_keys=image_secret_names(request.secrets),
        secrets=image_secret_names(request.secrets),
        gpu=request.gpu or None,
        image_id=request.image_id,
        ignore_python=request.ignore_python,
    )


def _image_spec_from_build(
    request: BuildImageRequest,
    *,
    context_digest: str | None,
) -> ImageSpec:
    return ImageSpec(
        architecture=request.architecture,
        base=request.existing_image_uri or "python:3.12-slim",
        python_version=request.python_version or "3.12",
        packages=list(request.python_packages),
        commands=list(request.commands),
        build_steps=[_build_step(step) for step in request.build_steps],
        env=parse_environment(request.env_vars),
        dockerfile=request.dockerfile or None,
        context_digest=context_digest,
        context_object_id=request.build_ctx_object or None,
        credential_keys=image_secret_names(request.secrets),
        secrets=image_secret_names(request.secrets),
        gpu=request.gpu or None,
        ignore_python=request.ignore_python,
    )


def _build_step(step: BuildStep) -> ImageBuildStep:
    kind = step.type.strip().lower()
    if kind == ImageBuildStepKind.Pip.value:
        return ImageBuildStep(kind=ImageBuildStepKind.Pip, args=step.command.split())
    if kind == ImageBuildStepKind.UvProject.value:
        return ImageBuildStep(kind=ImageBuildStepKind.UvProject, args=step.command.split())
    if kind == ImageBuildStepKind.Apt.value:
        return ImageBuildStep(kind=ImageBuildStepKind.Apt, args=step.command.split())
    if kind == ImageBuildStepKind.Micromamba.value:
        return ImageBuildStep(kind=ImageBuildStepKind.Micromamba, args=step.command.split())
    return ImageBuildStep(kind=ImageBuildStepKind.Shell, command=step.command)


def _source_image_for_spec(spec: ImageSpec) -> str:
    source = image_build_source_plan(spec)
    if source.source_image:
        return source.source_image
    if source.reference is not None:
        return source.reference.source_image
    return spec.base


def _resolved_credential_payload_for_spec(
    spec: ImageSpec,
    payloads: dict[str, ImageRegistryCredentialPayload],
) -> ImageRegistryCredentialPayload | None:
    source = image_build_source_plan(spec)
    if source.reference is None:
        return None
    return payloads.get(source.reference.registry)


def _build_source_credential_payload(
    payload: ImageRegistryCredentialPayload | None,
    *,
    required: bool,
) -> str | None:
    if not required or payload is None:
        return None
    return marshal_registry_credentials(payload)


def _stream_image_execution(
    services: ImageControlDependencies,
    spec: ImageSpec,
    *,
    workspace_id: str,
    request_id: str,
    credential_plan: ImageBuildCredentialPlan,
    registry_credential_payload: str | None,
    build_args: dict[str, str],
    python_version: str,
    image_id: str,
) -> Iterator[BuildImageResponse]:
    record = services.images.build(
        spec,
        workspace_id=workspace_id,
        request_id=request_id,
        credential_plan=credential_plan,
        registry_credential_payload=registry_credential_payload,
        build_args=build_args,
    )
    yield BuildImageResponse(
        build_id=record.id,
        image_id=record.image_id or "",
        python_version=record.image.python_version,
        status=record.status,
        phase=record.phase,
    )
    for event in stream_build_events(services.images, record.id, workspace_id=workspace_id):
        yield event.response


def stream_build_events(
    images: ImageBuildWorkflow,
    build_id: str,
    *,
    workspace_id: str,
    after: int = 0,
) -> Iterator[BuildImageEvent]:
    cursor = after
    record = images.get(build_id, workspace_id=workspace_id)
    heartbeat_at = time.monotonic() + 5
    while True:
        events = images.stream_events(build_id, workspace_id=workspace_id, after=cursor)
        for event in events:
            if event.sequence <= cursor:
                continue
            response = _response_from_stream_plan(event)
            cursor = event.sequence
            yield BuildImageEvent(sequence=event.sequence, response=response)
            if response.done:
                return
        if time.monotonic() >= heartbeat_at:
            heartbeat_at = time.monotonic() + 5
            yield BuildImageEvent(
                sequence=0,
                response=BuildImageResponse(
                    build_id=build_id,
                    image_id=record.image_id or "",
                    python_version=record.image.python_version,
                ),
            )
        time.sleep(IMAGE_BUILD_STREAM_POLL_SECONDS)


def _validate_secret_references(secrets: list[str]) -> None:
    if any("=" in secret for secret in secrets):
        raise InvalidInputError("image build secrets must reference stored secret names")


def _verify_reason(metadata_found: bool, exists: bool) -> str:
    if exists:
        return "image metadata found" if metadata_found else "image build found"
    if metadata_found:
        return f"image metadata is not current for clip version {CURRENT_IMAGE_CLIP_VERSION}"
    return "image metadata not found"


def _failed_build_response(
    error: Exception,
    *,
    image_id: str = "",
    python_version: str = "",
) -> BuildImageResponse:
    message = str(error) or error.__class__.__name__
    return _response_from_stream_plan(
        plan_image_build_failure_event(
            message,
            image_id=image_id,
            python_version=python_version,
        )
    )


def _responses_from_stream_plans(
    plans: list[ImageBuildStreamEventPlan],
) -> Iterator[BuildImageResponse]:
    for plan in plans:
        yield _response_from_stream_plan(plan)


def _response_from_stream_plan(plan: ImageBuildStreamEventPlan) -> BuildImageResponse:
    return BuildImageResponse(
        image_id=plan.image_id,
        build_id=plan.build_id,
        msg=plan.message,
        done=plan.done,
        success=plan.success,
        python_version=plan.python_version,
        warning=plan.warning,
        status=plan.status,
        phase=plan.phase,
        error=plan.error,
    )


__all__ = ["ImageControlService"]
