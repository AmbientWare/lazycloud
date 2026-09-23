from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Protocol

from pydantic import JsonValue
from shared.deployment_records import DeploymentSpec, VolumeMount
from shared.deployments import DeploymentKind
from shared.function_payloads import FunctionCloudpickleInvocation
from shared.http.deployments import DeploymentListResponse, DeploymentResponse
from shared.http.errors import HttpApiError
from shared.http.functions import (
    FunctionInvokeBody,
    FunctionInvokeResponse,
)
from shared.http.gateway import (
    Autoscaler,
    DeployStubRequest,
    DeployStubResponse,
    GatewayTaskPolicy,
    GatewayUrlKind,
    GetOrCreateStubRequest,
    GetOrCreateStubResponse,
    GetUrlRequest,
    GetUrlResponse,
    ResolveDeploymentTargetRequest,
    ResolveDeploymentTargetResponse,
    Schema,
    SchemaField,
    SecretVar,
    StubVolume,
)
from shared.http.objects import HeadObjectRequest, HeadObjectResponse, PutObjectResponse
from shared.http_transport import HttpChannel
from shared.image_building.authoring import ImageSpec

from lazycloud.abstractions.image import (
    Image,
    ImageBuildClient,
    ImageBuildOperation,
    ImageContextUploadResult,
)
from lazycloud.clients.gateway.control import GatewayControlClient
from lazycloud.clients.image.control import ImageControlClient
from lazycloud.control import ControlClientConfig, ControlClientConfigMixin, workspace_path
from lazycloud.control_clients import (
    control_http_channel,
    resource_control_client,
)
from lazycloud.function_results import FunctionResultDecodeError, decode_function_result
from lazycloud.json_contracts import validate_json_object
from lazycloud.references import HandlerReferenceError, source_root_handler_reference
from lazycloud.session.preparation import DeploymentPreparation
from lazycloud.session.task import Task, TaskClient, TaskSubscription
from lazycloud.session.uploads import (
    object_upload_timeout_seconds,
    stream_object_bytes,
    stream_object_file,
)
from lazycloud.source_sync import (
    SOURCE_PACKAGE_BUCKET,
    SOURCE_PACKAGE_CONTENT_TYPE,
    SourcePackageArchive,
)
from lazycloud.terminal import ProgressCallback, Terminal, TerminalStep
from lazycloud.values import cloudpickle_bytes


class DeploymentControlClient(Protocol):
    def get_or_create_stub(
        self,
        request: GetOrCreateStubRequest,
    ) -> GetOrCreateStubResponse: ...

    def deploy_stub(self, request: DeployStubRequest) -> DeployStubResponse: ...

    def get_url(self, request: GetUrlRequest) -> GetUrlResponse: ...

    def resolve_deployment_target(
        self,
        request: ResolveDeploymentTargetRequest,
    ) -> ResolveDeploymentTargetResponse: ...


class DeploymentResourceClient(Protocol):
    def list_deployments(
        self,
        *,
        active: bool | None = None,
        app_id: str | None = None,
        name: str | None = None,
        latest: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> DeploymentListResponse: ...

    def deployment(self, deployment_id: str) -> DeploymentResponse: ...

    def delete_deployment(self, deployment_id: str) -> None: ...

    def stop_deployment(self, deployment_id: str) -> DeploymentResponse: ...

    def start_deployment(self, deployment_id: str) -> DeploymentResponse: ...

    def scale_deployment(self, deployment_id: str, replicas: int) -> DeploymentResponse: ...


class ObjectUploadClient(Protocol):
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
    ) -> ImageContextUploadResult | Mapping[str, JsonValue]: ...

    def upload_source(
        self, archive: SourcePackageArchive, *, name: str, progress: ProgressCallback | None = None
    ) -> PutObjectResponse: ...


class DeploymentOperationError(RuntimeError):
    pass


class ImageBuildError(DeploymentOperationError):
    pass


@dataclass(frozen=True, slots=True)
class DeploymentSubmission:
    response: FunctionInvokeResponse
    task: Task | None = None

    @property
    def task_id(self) -> str:
        return self.response.task_id

    @property
    def output(self) -> str:
        return self.response.output

    @property
    def exit_code(self) -> int:
        return self.response.exit_code

    @property
    def done(self) -> bool:
        return self.response.done

    def result(self, *, wait: bool = False) -> object:
        if self.task is not None:
            result = self.task.result(wait=wait)
            if not result.ok:
                detail = f": {result.error}" if result.error else ""
                raise DeploymentOperationError(
                    f"deployment task {result.id} is {result.status.value}{detail}"
                )
            if result.value is None:
                return None
            try:
                return decode_function_result(result.value)
            except FunctionResultDecodeError as exc:
                raise DeploymentOperationError(str(exc)) from exc
        if self.response.result is not None:
            try:
                return decode_function_result(self.response.result)
            except FunctionResultDecodeError as exc:
                raise DeploymentOperationError(str(exc)) from exc
        return None

    def subscribe(self) -> TaskSubscription:
        if self.task is None:
            msg = "deployment submission did not create a task"
            raise DeploymentOperationError(msg)
        return self.task.subscribe()


@dataclass(slots=True)
class Deployment:
    deployment: DeploymentResponse
    client: DeploymentClient

    @property
    def id(self) -> str:
        return self.deployment.id

    @property
    def name(self) -> str:
        return self.deployment.name

    @property
    def stub_id(self) -> str:
        return self.deployment.stub_id or ""

    def invoke_url(
        self,
        *,
        port: int | None = None,
        url_type: GatewayUrlKind = GatewayUrlKind.Deployment,
    ) -> str:
        return self.client.invoke_url(self.deployment, port=port, url_type=url_type)

    def submit(
        self,
        *args: object,
        kwargs: dict[str, object] | None = None,
    ) -> DeploymentSubmission:
        return self.client.submit(self.deployment, *args, kwargs=kwargs)

    def subscribe(
        self,
        *args: object,
        kwargs: dict[str, object] | None = None,
    ) -> TaskSubscription:
        return self.submit(*args, kwargs=kwargs).subscribe()


@dataclass(slots=True)
class DeploymentClient(ControlClientConfigMixin):
    client: DeploymentControlClient | None = None
    resource_client: DeploymentResourceClient | None = None
    image_client: ImageBuildClient | None = None
    object_client: ObjectUploadClient | None = None
    workspace: str | None = None
    endpoint: str | None = None
    token: str | None = None
    timeout_seconds: float = 10.0
    sync_source: bool = False
    source_root: str | Path | None = None
    source_ignore_patterns: tuple[str, ...] = ()
    source_include_patterns: tuple[str, ...] = ()
    terminal: Terminal | None = None
    preparation: DeploymentPreparation | None = field(default=None, repr=False)
    _channel: HttpChannel | None = field(default=None, init=False, repr=False)
    _client_lock: RLock = field(default_factory=RLock, init=False, repr=False)

    @property
    def control_client(self) -> DeploymentControlClient:
        if self.client is None:
            self.client = GatewayControlClient(
                channel=self._http_channel(), workspace=self._config().workspace
            )
        return self.client

    @property
    def resources(self) -> DeploymentResourceClient:
        if self.resource_client is None:
            self.resource_client = resource_control_client(self._config())
        return self.resource_client

    def create(
        self,
        spec: DeploymentSpec,
        *,
        workspace: str | None = None,
        external_url: str | None = None,
        image: Image | None = None,
        sync_source: bool | None = None,
        source_root: str | Path | None = None,
    ) -> DeployStubResponse:
        config = self._config()
        selected_workspace = workspace or config.workspace
        stub = self.prepare(
            spec,
            workspace=selected_workspace,
            image=image,
            sync_source=sync_source,
            source_root=source_root,
        )
        response = self.control_client.deploy_stub(
            DeployStubRequest(
                stub_id=stub.stub_id,
                name=spec.name,
                workspace=selected_workspace,
                external_url=external_url or config.endpoint,
            )
        )
        response.stub_id = response.stub_id or stub.stub_id
        return response

    def prepare(
        self,
        spec: DeploymentSpec,
        *,
        workspace: str | None = None,
        image: Image | None = None,
        sync_source: bool | None = None,
        source_root: str | Path | None = None,
    ) -> GetOrCreateStubResponse:
        selected_workspace = workspace or self._config().workspace
        selected_root = source_root or self.source_root
        archive_prefix: tuple[str, ...] = ()
        # The source archive falls back to the working directory, so the handler is
        # checked against that same root even when no root was named. Validating only
        # on an explicit root lets an unimportable handler deploy successfully and fail
        # at request time, where the failure carries no diagnosis.
        selected_sync = self.sync_source if sync_source is None else sync_source
        validation_root = (
            selected_root if selected_root is not None else ("." if selected_sync else None)
        )
        if validation_root is not None and spec.handler:
            try:
                source_reference = source_root_handler_reference(
                    spec.handler,
                    Path(validation_root).expanduser().resolve(),
                )
            except HandlerReferenceError as exc:
                raise DeploymentOperationError(str(exc)) from exc
            archive_prefix = source_reference.archive_prefix
            spec = spec.model_copy(update={"handler": source_reference.handler})
            if selected_root is not None:
                selected_root = source_reference.root
        elif selected_root is not None:
            selected_root = Path(selected_root).expanduser().resolve()
            if not selected_root.is_dir():
                msg = f"deployment source root is not a directory: {selected_root}"
                raise RuntimeError(msg)
        source_image = deepcopy(image) if image is not None else _image_from_spec(spec.image)
        if source_image.context_path is not None:
            source_image.context_path = str(Path(source_image.context_path).expanduser().resolve())
        scope = self._preparation_scope(selected_workspace)
        with (
            nullcontext(self.preparation)
            if self.preparation is not None
            else DeploymentPreparation()
        ) as preparation:
            image_future = (
                preparation.image(
                    _preparation_key(
                        {
                            "scope": scope,
                            "image": source_image.spec().model_dump(mode="json"),
                            "credentials": {**source_image.get_credentials_from_env()},
                        }
                    ),
                    lambda: self._prepare_image(source_image),
                )
                if self.client is None or self.image_client is not None
                else None
            )
            source_object_id = self._source_object_id(
                spec,
                sync_source=sync_source,
                source_root=selected_root,
                archive_prefix=archive_prefix,
                preparation=preparation,
                scope=scope,
            )
            prepared_spec = (
                spec.model_copy(update={"image": image_future.result()})
                if image_future is not None
                else spec
            )
        with self._step("Runtime", prepared_spec.name) as step:
            response = self.control_client.get_or_create_stub(
                _stub_request_from_spec(
                    prepared_spec,
                    workspace=selected_workspace,
                    object_id=source_object_id,
                )
            )
            step.done(f"{prepared_spec.name} · {response.stub_id[:8]}")
        return response

    def list(
        self,
        *,
        id: str | None = None,
        name: str | None = None,
        status: str | None = None,
        filters: dict[str, list[str]] | None = None,
        limit: int = 100,
        workspace: str | None = None,
    ) -> list[DeploymentResponse]:
        selected_filters = dict(filters or {})
        if id:
            selected_filters["id"] = [id]
        if name:
            selected_filters["name"] = [name]
        if status:
            selected_filters["status"] = [status]
        selected_workspace = workspace or self._config().workspace
        resources = self._resource_client_for_workspace(selected_workspace)
        active = None
        if values := selected_filters.get("status"):
            active = values[0].lower() in {"active", "running", "true"}
        response = resources.list_deployments(
            active=active,
            app_id=_first_filter(selected_filters, "app_id")
            or _first_filter(selected_filters, "app"),
            name=_first_filter(selected_filters, "name"),
            limit=limit,
        )
        deployments = response.data
        if deployment_id := _first_filter(selected_filters, "id"):
            deployments = [item for item in deployments if item.id == deployment_id]
        return deployments

    def get(
        self,
        deployment_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> DeploymentResponse:
        selected_workspace = workspace or self._config().workspace
        resources = self._resource_client_for_workspace(selected_workspace)
        try:
            return resources.deployment(deployment_id_or_name)
        except HttpApiError as exc:
            if exc.status_code != 404:
                raise
        by_name = self.list(
            filters={"name": [deployment_id_or_name]},
            limit=1,
            workspace=workspace,
        )
        if by_name:
            return by_name[0]
        msg = f"deployment not found: {deployment_id_or_name}"
        raise DeploymentOperationError(msg)

    def handle(
        self,
        deployment_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> Deployment:
        return Deployment(
            deployment=self.get(deployment_id_or_name, workspace=workspace),
            client=self,
        )

    def invoke_url(
        self,
        deployment: DeploymentResponse | str,
        *,
        workspace: str | None = None,
        port: int | None = None,
        url_type: GatewayUrlKind = GatewayUrlKind.Deployment,
    ) -> str:
        selected_workspace = workspace or self._config().workspace
        selected = self._deployment_view(deployment, workspace=selected_workspace)
        response = self.control_client.get_url(
            GetUrlRequest(
                stub_id=selected.stub_id or "",
                deployment_id=selected.id,
                url_type=url_type,
                workspace=selected_workspace,
                external_url=self._config().endpoint,
                port=port,
            )
        )
        return response.url

    def resolve_target(
        self,
        *,
        kind: DeploymentKind,
        name: str,
        app: str | None = None,
        deployment_version: int | None = None,
        workspace: str | None = None,
    ) -> ResolveDeploymentTargetResponse:
        config = self._config()
        response = self.control_client.resolve_deployment_target(
            ResolveDeploymentTargetRequest(
                kind=kind,
                name=name,
                app=app or "",
                workspace=workspace or config.workspace,
                deployment_version=deployment_version,
                external_url=config.endpoint,
            )
        )
        return response

    def submit(
        self,
        deployment: DeploymentResponse | str,
        *args: object,
        kwargs: dict[str, object] | None = None,
        workspace: str | None = None,
    ) -> DeploymentSubmission:
        selected_workspace = workspace or self._config().workspace
        selected = self._deployment_view(deployment, workspace=selected_workspace)
        if not selected.stub_id:
            msg = f"deployment {selected.id} does not include a stub_id"
            raise DeploymentOperationError(msg)
        payload = cloudpickle_bytes({"args": args, "kwargs": kwargs or {}})
        request = FunctionInvokeBody(
            stub_id=selected.stub_id,
            headless=True,
            invocation=FunctionCloudpickleInvocation.from_bytes(payload),
        )
        response = FunctionInvokeResponse.model_validate(
            self._http_channel().post(
                workspace_path("/api/v1/functions/invoke", selected_workspace),
                request.model_dump(mode="json"),
            )
        )
        task = (
            TaskClient(
                workspace=selected_workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
            ).handle(response.task_id)
            if response.task_id
            else None
        )
        return DeploymentSubmission(response=response, task=task)

    def _deployment_view(
        self,
        deployment: DeploymentResponse | str,
        *,
        workspace: str,
    ) -> DeploymentResponse:
        if isinstance(deployment, str):
            return self.get(deployment, workspace=workspace)
        return deployment

    def subscribe(
        self,
        deployment: DeploymentResponse | str,
        *args: object,
        kwargs: dict[str, object] | None = None,
        workspace: str | None = None,
    ) -> TaskSubscription:
        return self.submit(
            deployment,
            *args,
            kwargs=kwargs,
            workspace=workspace,
        ).subscribe()

    def delete(
        self,
        deployment_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> None:
        selected_workspace = workspace or self._config().workspace
        deployment = self.get(deployment_id_or_name, workspace=selected_workspace)
        self._resource_client_for_workspace(selected_workspace).delete_deployment(deployment.id)

    def stop(
        self,
        deployment_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> DeploymentResponse:
        selected_workspace = workspace or self._config().workspace
        deployment = self.get(deployment_id_or_name, workspace=selected_workspace)
        return self._resource_client_for_workspace(selected_workspace).stop_deployment(
            deployment.id
        )

    def start(
        self,
        deployment_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> DeploymentResponse:
        selected_workspace = workspace or self._config().workspace
        deployment = self.get(deployment_id_or_name, workspace=selected_workspace)
        return self._resource_client_for_workspace(selected_workspace).start_deployment(
            deployment.id
        )

    def scale(
        self,
        deployment_id_or_name: str,
        containers: int,
        *,
        workspace: str | None = None,
    ) -> DeploymentResponse:
        selected_workspace = workspace or self._config().workspace
        deployment = self.get(deployment_id_or_name, workspace=selected_workspace)
        return self._resource_client_for_workspace(selected_workspace).scale_deployment(
            deployment.id,
            containers,
        )

    def _resource_client_for_workspace(self, workspace: str) -> DeploymentResourceClient:
        if workspace == self._config().workspace:
            return self.resources
        config = self._config()
        return resource_control_client(
            ControlClientConfig(
                endpoint=config.endpoint,
                token=config.token,
                workspace=workspace,
                timeout_seconds=config.timeout_seconds,
            )
        )

    def _http_channel(self) -> HttpChannel:
        with self._client_lock:
            if self._channel is None:
                self._channel = control_http_channel(self._config())
            return self._channel

    def _prepare_image(self, image: Image) -> ImageSpec:
        if _needs_context_upload(image):
            image._sync_context(self._object_client())

        with ImageBuildOperation(image, self._image_client(), terminal=self.terminal) as operation:
            operation.verify()
            result = operation.finish()
        if not result.success:
            msg = result.error or "image build failed"
            if result.build_id:
                msg = f"{msg} (build {result.build_id})"
            raise ImageBuildError(msg)

        return operation.image.spec().model_copy(
            update={
                "image_id": result.image_id or operation.image.spec().image_id,
                "python_version": result.python_version or operation.image.spec().python_version,
            }
        )

    def _preparation_scope(self, workspace: str) -> str:
        config = self._config()
        return _preparation_key(
            {
                "endpoint": config.endpoint,
                "workspace": workspace,
                "token": config.token,
                "timeout": config.timeout_seconds,
            }
        )

    def _step(self, name: str, summary: str) -> AbstractContextManager[TerminalStep]:
        if self.terminal is None:
            return nullcontext(
                TerminalStep(name=name, terminal=Terminal(quiet=True), summary=summary)
            )
        return self.terminal.step(name, summary)

    def _image_client(self) -> ImageBuildClient:
        if self.image_client is None:
            config = self._config()
            self.image_client = ImageControlClient(
                channel=self._http_channel(),
                workspace=config.workspace,
                timeout_seconds=config.timeout_seconds,
            )
        return self.image_client

    def _source_object_id(
        self,
        spec: DeploymentSpec,
        *,
        sync_source: bool | None,
        source_root: str | Path | None,
        archive_prefix: tuple[str, ...],
        preparation: DeploymentPreparation,
        scope: str,
    ) -> str:
        selected_sync = self.sync_source if sync_source is None else sync_source
        metadata_object_id = _metadata_str(spec.metadata, "object_id")
        if not selected_sync:
            return metadata_object_id

        selected_root = source_root or self.source_root
        if self.client is not None and self.object_client is None and selected_root is None:
            return metadata_object_id

        from lazycloud.source_sync import (
            SOURCE_IGNORE_FILE_WRITTEN_NOTICE,
            SourcePackageSyncer,
            ensure_source_ignore_file,
        )

        root = Path(selected_root or ".").expanduser().resolve()
        if ensure_source_ignore_file(root) and self.terminal is not None:
            self.terminal.detail(SOURCE_IGNORE_FILE_WRITTEN_NOTICE)
        syncer = SourcePackageSyncer(
            self._object_client(),
            root_dir=root,
            archive_prefix=archive_prefix,
            terminal=self.terminal,
        )
        return preparation.source(
            _preparation_key(
                {
                    "scope": scope,
                    "root": str(root),
                    "prefix": list(archive_prefix),
                    "ignore": list(self.source_ignore_patterns),
                    "include": list(self.source_include_patterns),
                }
            ),
            lambda: (
                syncer.sync(
                    ignore_patterns=self.source_ignore_patterns or None,
                    include_patterns=self.source_include_patterns or None,
                ).object_id
            ),
        )

    def _object_client(self) -> ObjectUploadClient:
        with self._client_lock:
            if self.object_client is None:
                self.object_client = _DefaultObjectUploadClient(
                    self._config(), channel=self._http_channel()
                )
            return self.object_client


@dataclass(frozen=True, slots=True)
class _DefaultObjectUploadClient:
    config: ControlClientConfig
    channel: HttpChannel

    def upload_source(
        self, archive: SourcePackageArchive, *, name: str, progress: ProgressCallback | None = None
    ) -> PutObjectResponse:
        response = HeadObjectResponse.model_validate(
            self.channel.post(
                workspace_path("/gateway/objects/head", self.config.workspace),
                HeadObjectRequest(hash=archive.sha256, bucket=SOURCE_PACKAGE_BUCKET).model_dump(
                    mode="json"
                ),
            )
        )
        if response.exists:
            if progress is not None:
                progress(archive.size)
            return PutObjectResponse(object_id=response.object_id)
        return stream_object_file(
            channel=self.channel,
            workspace=self.config.workspace,
            source=archive.path,
            size=archive.size,
            object_hash=archive.sha256,
            name=name,
            bucket=SOURCE_PACKAGE_BUCKET,
            overwrite=False,
            content_type=SOURCE_PACKAGE_CONTENT_TYPE,
            metadata={
                "kind": "source-package",
                "sha256": archive.sha256,
                "file_count": str(len(archive.files)),
            },
            timeout_seconds=object_upload_timeout_seconds(self.config.timeout_seconds),
            progress=progress,
        )

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
    ) -> PutObjectResponse:
        object_hash = hashlib.sha256(data).hexdigest()
        upload_timeout_seconds = object_upload_timeout_seconds(self.config.timeout_seconds)
        if not overwrite:
            response = HeadObjectResponse.model_validate(
                self.channel.post(
                    workspace_path("/gateway/objects/head", self.config.workspace),
                    HeadObjectRequest(hash=object_hash, bucket=bucket).model_dump(mode="json"),
                )
            )
            if response.exists:
                if progress is not None:
                    progress(len(data))
                return PutObjectResponse(object_id=response.object_id)
        uploaded = stream_object_bytes(
            channel=self.channel,
            workspace=self.config.workspace,
            timeout_seconds=upload_timeout_seconds,
            data=data,
            name=name,
            object_hash=object_hash,
            bucket=bucket,
            overwrite=overwrite,
            content_type=content_type,
            metadata=metadata,
            progress=progress,
        )
        return uploaded


def _stub_request_from_spec(
    spec: DeploymentSpec,
    *,
    workspace: str,
    object_id: str = "",
) -> GetOrCreateStubRequest:
    image = spec.image
    metadata = validate_json_object(spec.metadata)
    return GetOrCreateStubRequest(
        object_id=object_id,
        image_id=image.image_id or "",
        stub_type=spec.kind.value,
        name=spec.name,
        python_version=image.python_version,
        image_base=image.base,
        python_packages=list(image.packages),
        image_commands=list(image.commands),
        image_build_steps=[step.model_dump(mode="json") for step in image.build_steps],
        image_env=[f"{key}={value}" for key, value in image.env.items()],
        image_workdir=image.workdir,
        image_dockerfile=image.dockerfile or "",
        image_context_path=image.context_path or "",
        image_context_digest=image.context_digest or "",
        image_context_object=image.context_object_id or "",
        image_include_patterns=list(image.include_files_patterns),
        image_credential_keys=list(image.credential_keys),
        image_secrets=list(image.secrets),
        image_gpu=image.gpu or "",
        image_ignore_python=image.ignore_python,
        cpu=spec.resources.cpu,
        memory=spec.resources.memory,
        disk=spec.resources.disk,
        gpu=list(spec.resources.gpu),
        gpu_count=spec.resources.gpu_count,
        handler=spec.handler or "",
        route=spec.route,
        domain=spec.domain,
        methods=list(spec.methods),
        cron=spec.cron or "",
        retry_policy=spec.retry_policy,
        timeout=spec.resources.timeout_seconds or _metadata_int(metadata, "timeout"),
        workers=_metadata_int(metadata, "workers"),
        max_pending_tasks=_metadata_int(metadata, "max_pending_tasks"),
        keep_warm_seconds=spec.resources.keep_warm,
        concurrent_requests=spec.resources.concurrency,
        in_process=_metadata_bool(metadata, "in_process"),
        env=[f"{key}={value}" for key, value in spec.env.items()],
        secrets=[SecretVar(name=name) for name in spec.secrets],
        volumes=[
            StubVolume(
                id=volume.name,
                mount_path=volume.mount_path,
                config=_stub_volume_config(volume),
            )
            for volume in spec.volumes
        ],
        ports=list(spec.ports.values()),
        command=list(spec.command),
        callback_url=_metadata_str(metadata, "callback_url"),
        authorized=_metadata_bool(metadata, "authorized"),
        lifecycle_hooks=spec.lifecycle_hooks,
        autoscaler=_deployment_autoscaler(spec, metadata),
        task_policy=_metadata_task_policy(metadata, spec),
        checkpoint_enabled=_metadata_bool(metadata, "checkpoint_enabled"),
        metadata=metadata,
        client_contract=spec.client_contract,
        app_name=_metadata_str(metadata, "app"),
        inputs=_metadata_schema(metadata, "inputs"),
        outputs=_metadata_schema(metadata, "outputs"),
        tcp=_metadata_bool(metadata, "tcp"),
        block_network=_metadata_bool(metadata, "block_network"),
        allow_list=_metadata_str_list(metadata, "allow_list"),
        docker_enabled=_metadata_bool(metadata, "docker_enabled"),
        preemptible=spec.resources.preemptible,
        machine=_metadata_str(metadata, "machine"),
        region=spec.resources.region,
        availability_zone=spec.resources.availability_zone,
        workspace=workspace,
    )


def _stub_volume_config(volume: VolumeMount) -> dict[str, JsonValue]:
    config = validate_json_object(volume.config or {})
    config["read_only"] = volume.read_only
    return config


def _image_from_spec(spec: ImageSpec) -> Image:
    restored = Image(
        base_image=spec.base,
        python_version=spec.python_version,
        python_packages=tuple(spec.packages),
        commands=tuple(spec.commands),
        image_id=spec.image_id,
    )
    restored.build_steps = tuple(spec.build_steps)
    restored.env_vars = tuple(spec.env.items())
    restored.workdir_path = spec.workdir
    restored.dockerfile_content = spec.dockerfile
    restored.context_path = spec.context_path
    restored.context_digest = spec.context_digest
    restored.credential_keys = tuple(spec.credential_keys)
    restored.secrets = tuple(spec.secrets)
    restored.gpu_hint = spec.gpu
    restored.ignore_python = spec.ignore_python
    restored.include_files_patterns = tuple(spec.include_files_patterns)
    restored.context_object_id = spec.context_object_id
    return restored


def _needs_context_upload(image: Image) -> bool:
    spec = image.spec()
    return bool((spec.context_path or spec.dockerfile) and not spec.context_object_id)


def _preparation_key(payload: dict[str, JsonValue]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _metadata_int(metadata: Mapping[str, JsonValue], key: str) -> int:
    value = metadata.get(key)
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        return int(value)
    return 0


def _metadata_str(metadata: Mapping[str, JsonValue], key: str) -> str:
    value = metadata.get(key)
    return value if isinstance(value, str) else ""


def _metadata_bool(metadata: Mapping[str, JsonValue], key: str) -> bool:
    value = metadata.get(key)
    return value if isinstance(value, bool) else False


def _metadata_mapping(metadata: Mapping[str, JsonValue], key: str) -> dict[str, JsonValue]:
    value = metadata.get(key)
    if isinstance(value, dict):
        return dict(value)
    return {}


def _metadata_str_list(metadata: Mapping[str, JsonValue], key: str) -> list[str]:
    value = metadata.get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _deployment_autoscaler(
    spec: DeploymentSpec,
    metadata: Mapping[str, JsonValue],
) -> Autoscaler:
    value = _metadata_mapping(metadata, "autoscaler")
    if not value:
        return Autoscaler(tasks_per_container=_default_tasks_per_container(spec, metadata))
    return Autoscaler.model_validate(value)


def _default_tasks_per_container(
    spec: DeploymentSpec,
    metadata: Mapping[str, JsonValue],
) -> int:
    if spec.kind in {DeploymentKind.Endpoint, DeploymentKind.Asgi}:
        return max(_metadata_int(metadata, "workers"), 1) * spec.resources.concurrency
    return 1


def _metadata_task_policy(
    metadata: Mapping[str, JsonValue],
    spec: DeploymentSpec,
) -> GatewayTaskPolicy:
    value = _metadata_mapping(metadata, "task_policy")
    return GatewayTaskPolicy(
        timeout=_int_value(value.get("timeout") or value.get("timeout_seconds"))
        or spec.resources.timeout_seconds
        or 0,
        ttl=_int_value(value.get("ttl") or value.get("ttl_seconds")),
    )


def _metadata_schema(metadata: Mapping[str, JsonValue], key: str) -> Schema:
    return _schema_from_mapping(_metadata_mapping(metadata, key))


def _schema_from_mapping(value: dict[str, JsonValue]) -> Schema:
    raw_fields = value.get("fields")
    if not isinstance(raw_fields, dict):
        raw_fields = value
    fields: dict[str, SchemaField] = {}
    for name, field_value in raw_fields.items():
        if not isinstance(name, str):
            continue
        fields[name] = _schema_field(field_value)
    return Schema(fields=fields)


def _schema_field(value: JsonValue) -> SchemaField:
    if isinstance(value, str):
        return SchemaField(type=value)
    if isinstance(value, dict):
        field_type = value.get("type", "")
        nested = value.get("fields")
        nested_fields: dict[str, SchemaField] = {}
        if isinstance(nested, dict):
            nested_fields = _schema_from_mapping({"fields": nested}).fields
        return SchemaField(type=str(field_type), fields=nested_fields)
    return SchemaField(type="")


def _int_value(value: object) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        return int(value)
    return 0


def _first_filter(filters: dict[str, list[str]], key: str) -> str | None:
    values = filters.get(key)
    return values[0] if values else None


__all__ = [
    "Deployment",
    "DeploymentClient",
    "DeploymentControlClient",
    "DeploymentOperationError",
    "DeploymentResourceClient",
    "DeploymentSubmission",
]
