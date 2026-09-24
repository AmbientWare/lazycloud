from __future__ import annotations

import shutil
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import Thread
from uuid import uuid4

from pydantic import ValidationError
from shared.contracts import ContractModel
from shared.deployments import StubKind
from shared.http.workspace_sync import WorkspaceSyncBatch

from worker.container_client.models import (
    ContainerCheckpointRequest,
    ContainerCheckpointResponse,
    ContainerExecRequest,
    ContainerExecResponse,
    ContainerKillRequest,
    ContainerKillResponse,
    ContainerLogEntry,
    ContainerSandboxCreateDirectoryRequest,
    ContainerSandboxCreateDirectoryResponse,
    ContainerSandboxDeleteDirectoryRequest,
    ContainerSandboxDeleteDirectoryResponse,
    ContainerSandboxDeleteFileRequest,
    ContainerSandboxDeleteFileResponse,
    ContainerSandboxDownloadFileRequest,
    ContainerSandboxDownloadFileResponse,
    ContainerSandboxExecRequest,
    ContainerSandboxExecResponse,
    ContainerSandboxExposePortRequest,
    ContainerSandboxExposePortResponse,
    ContainerSandboxFindInFilesRequest,
    ContainerSandboxFindInFilesResponse,
    ContainerSandboxKillRequest,
    ContainerSandboxKillResponse,
    ContainerSandboxListExposedPortsRequest,
    ContainerSandboxListExposedPortsResponse,
    ContainerSandboxListFilesRequest,
    ContainerSandboxListFilesResponse,
    ContainerSandboxListProcessesRequest,
    ContainerSandboxListProcessesResponse,
    ContainerSandboxProcessInfo,
    ContainerSandboxReplaceInFilesRequest,
    ContainerSandboxReplaceInFilesResponse,
    ContainerSandboxStatFileRequest,
    ContainerSandboxStatFileResponse,
    ContainerSandboxStatusRequest,
    ContainerSandboxStatusResponse,
    ContainerSandboxStderrRequest,
    ContainerSandboxStderrResponse,
    ContainerSandboxStdoutRequest,
    ContainerSandboxStdoutResponse,
    ContainerSandboxUnexposePortRequest,
    ContainerSandboxUnexposePortResponse,
    ContainerSandboxUpdateNetworkPermissionsRequest,
    ContainerSandboxUpdateNetworkPermissionsResponse,
    ContainerSandboxUploadFileRequest,
    ContainerSandboxUploadFileResponse,
    ContainerStatusRequest,
    ContainerStatusResponse,
    ContainerStreamLogsRequest,
    SyncContainerWorkspaceResponse,
)
from worker.container_service.models import (
    CONTAINER_NOT_FOUND_MESSAGE,
    SANDBOX_FILESYSTEM_OVER_LIMIT_EXIT,
    SANDBOX_PROCESS_MANAGER_NOT_READY_MESSAGE,
    SandboxDownloadReport,
    SandboxFileOverLimitError,
    SandboxFilesystemRequest,
    SandboxProcessEvent,
    SandboxProcessEventType,
    WorkerContainerServiceInstance,
)
from worker.container_service.protocols import (
    BridgeSandboxPortPublisher,
    WorkerContainerCheckpointCreator,
    WorkerContainerInstanceStore,
    WorkerContainerRuntimeController,
    WorkerSandboxDockerLifecycle,
    WorkerSandboxLogSink,
    WorkerSandboxNetworkPolicyUpdater,
    WorkerSandboxPortPublisher,
    WorkerSandboxProcessManager,
    WorkerSandboxProcessManagerFactory,
)
from worker.execution import (
    plan_container_exec,
    plan_container_kill,
    plan_sandbox_exec,
)
from worker.sandbox_server import (
    WORKER_CONTAINER_UPLOADS_HOST_PATH,
    WORKER_CONTAINER_UPLOADS_MOUNT_PATH,
    SandboxExposePortRequest,
    SandboxFileOperation,
    plan_sandbox_expose_port,
    plan_sandbox_list_exposed_ports,
    plan_sandbox_process_log_ack,
    plan_sandbox_status,
    resolve_sandbox_container_path,
)
from worker.workspace_sync import apply_workspace_batch


@dataclass(slots=True)
class WorkerContainerService:
    instances: WorkerContainerInstanceStore
    process_managers: WorkerSandboxProcessManagerFactory | None = None
    sandbox_docker: WorkerSandboxDockerLifecycle | None = None
    runtime: WorkerContainerRuntimeController | None = None
    logs: WorkerSandboxLogSink | None = None
    checkpoints: WorkerContainerCheckpointCreator | None = None
    network_policy: WorkerSandboxNetworkPolicyUpdater | None = None
    ports: WorkerSandboxPortPublisher = field(default_factory=BridgeSandboxPortPublisher)

    def prepare_workload(self, container_id: str) -> None:
        manager = self._ready_process_manager(self._required_instance(container_id))
        if isinstance(manager, str):
            raise RuntimeError(manager)
        try:
            manager.start_workload()
        finally:
            manager.cleanup()

    def container_status(self, request: ContainerStatusRequest) -> ContainerStatusResponse:
        instance = self._instance(request.container_id)
        if instance is None:
            return ContainerStatusResponse(ok=False, error_msg=CONTAINER_NOT_FOUND_MESSAGE)
        if instance.build_request and instance.status:
            return ContainerStatusResponse(
                ok=True,
                status=instance.status,
                exit_code=instance.exit_code,
                build_archive_object_key=instance.build_archive_object_key,
                build_archive_size_bytes=instance.build_archive_size_bytes,
                build_archive_sha256=instance.build_archive_sha256,
                error_msg=instance.build_error_message,
            )
        runtime_status = (
            self.runtime.status(request.container_id)
            if self.runtime is not None
            else ("running" if instance.sandbox_process_manager_ready else "created")
        )
        return ContainerStatusResponse(
            ok=True,
            status=runtime_status,
            exit_code=0,
        )

    def container_exec(self, request: ContainerExecRequest) -> ContainerExecResponse:
        instance = self._instance(request.container_id)
        if instance is None:
            return ContainerExecResponse(ok=False, error_msg=CONTAINER_NOT_FOUND_MESSAGE)
        if self.runtime is None:
            return ContainerExecResponse(ok=False, error_msg="container runtime is not configured")
        plan = plan_container_exec(
            request.container_id,
            request.command,
            instance_env=instance.env,
            request_env=list(request.env),
            build_secret_env=instance.build_secret_env,
            cwd=instance.cwd,
            build_request=instance.build_request,
        )
        return self.runtime.exec_container(
            request.container_id,
            argv=plan.argv,
            env=plan.env,
            cwd=plan.cwd or instance.cwd,
        )

    def container_kill(self, request: ContainerKillRequest) -> ContainerKillResponse:
        if self._instance(request.container_id) is None:
            return ContainerKillResponse(ok=False, error_msg=CONTAINER_NOT_FOUND_MESSAGE)
        if self.runtime is None:
            return ContainerKillResponse(ok=False, error_msg="container runtime is not configured")
        plan = plan_container_kill(request.container_id)
        try:
            if self.sandbox_docker is not None:
                with suppress(Exception):
                    self.sandbox_docker.stop(request.container_id)
            self.runtime.kill_container(
                request.container_id,
                signal=plan.signal or 15,
                force_delete=plan.force_delete,
            )
        except RuntimeError as exc:
            if _container_runtime_missing(exc):
                return ContainerKillResponse(ok=True)
            return ContainerKillResponse(ok=False, error_msg=str(exc))
        return ContainerKillResponse(ok=True)

    def container_checkpoint(
        self,
        request: ContainerCheckpointRequest,
    ) -> ContainerCheckpointResponse:
        instance = self._instance(request.container_id)
        if instance is None:
            return ContainerCheckpointResponse(ok=False, error_msg=CONTAINER_NOT_FOUND_MESSAGE)
        if self.checkpoints is None:
            return ContainerCheckpointResponse(
                ok=False,
                error_msg="checkpoint creator is not configured",
            )
        streams_suspended = False
        try:
            if instance.stub_type == StubKind.Sandbox and self.process_managers is not None:
                streams_suspended = True
                self.process_managers.suspend_process_streams(instance)
            checkpoint_id = self.checkpoints.create_checkpoint(
                instance,
                checkpoint_id=request.checkpoint_id,
            )
        except Exception as exc:
            return ContainerCheckpointResponse(ok=False, error_msg=str(exc))
        finally:
            if streams_suspended and self.process_managers is not None:
                self.process_managers.resume_process_streams(instance)
        return ContainerCheckpointResponse(ok=True, checkpoint_id=checkpoint_id)

    def stream_logs(self, request: ContainerStreamLogsRequest) -> Iterable[ContainerLogEntry]:
        instance = self._instance(request.container_id)
        if instance is None:
            return ()
        if instance.build_request:
            return _stream_build_request_logs(request.container_id, self.instances)
        return tuple(ContainerLogEntry(msg=message) for message in instance.log_messages)

    def sandbox_exec(self, request: ContainerSandboxExecRequest) -> ContainerSandboxExecResponse:
        instance = self._instance(request.container_id)
        if instance is None:
            return ContainerSandboxExecResponse(ok=False, error_msg=CONTAINER_NOT_FOUND_MESSAGE)
        manager = self._process_manager(instance)
        if manager is None:
            return ContainerSandboxExecResponse(
                ok=False,
                error_msg="sandbox process manager is not configured",
            )
        plan = plan_sandbox_exec(
            request.container_id,
            request.command,
            instance_env=instance.env,
            extra_env=request.env,
            cwd=request.cwd or instance.cwd,
        )
        if not plan.ok:
            return ContainerSandboxExecResponse(ok=False, error_msg=plan.error_message)

        events = iter(
            manager.stream_exec(
                plan.argv,
                cwd=plan.cwd or instance.cwd,
                env=plan.env,
            )
        )
        handed_off = False
        try:
            for event in events:
                if event.event_type is SandboxProcessEventType.Started:
                    instance.sandbox_process_manager_ready = True
                    self.instances.save_container_instance(instance)
                    Thread(
                        target=self._consume_sandbox_process_events,
                        args=(
                            manager,
                            events,
                            instance,
                            plan.argv,
                            plan.cwd or "",
                        ),
                        name=f"sandbox-exec-{instance.container_id}-{event.pid}",
                        daemon=True,
                    ).start()
                    handed_off = True
                    return ContainerSandboxExecResponse(ok=True, pid=event.pid)
                if event.event_type is SandboxProcessEventType.Chunk:
                    error = self._append_process_log(instance, event, plan.argv, plan.cwd or "")
                    manager.ack(event.pid, event.seq, ok=not error)
                    if error:
                        return ContainerSandboxExecResponse(ok=False, error_msg=error)
                    continue
                if event.event_type is SandboxProcessEventType.Exited:
                    return ContainerSandboxExecResponse(
                        ok=False,
                        error_msg=event.error_message
                        or "sandbox process exited before start acknowledgement",
                    )
        except Exception as exc:
            return ContainerSandboxExecResponse(ok=False, error_msg=str(exc))
        finally:
            if not handed_off:
                manager.cleanup()

        return ContainerSandboxExecResponse(
            ok=False,
            error_msg="sandbox exec stream closed before process start",
        )

    def sandbox_status(
        self,
        request: ContainerSandboxStatusRequest,
    ) -> ContainerSandboxStatusResponse:
        instance = self._instance(request.container_id)
        if instance is None:
            return ContainerSandboxStatusResponse(ok=False, error_msg=CONTAINER_NOT_FOUND_MESSAGE)
        if request.pid == 0:
            manager = self._ready_process_manager(instance)
            if isinstance(manager, str):
                return ContainerSandboxStatusResponse(ok=False, error_msg=manager)
            manager.cleanup()
            plan = plan_sandbox_status(manager_ready=True)
            return ContainerSandboxStatusResponse(
                ok=plan.ok,
                status=plan.status.value,
                exit_code=plan.exit_code,
                error_msg=plan.error_message,
            )
        manager = self._ready_process_manager(instance)
        if isinstance(manager, str):
            return ContainerSandboxStatusResponse(ok=False, error_msg=manager)
        try:
            exit_code = manager.status(request.pid)
        except Exception as exc:
            return ContainerSandboxStatusResponse(ok=False, error_msg=str(exc))
        finally:
            manager.cleanup()
        plan = plan_sandbox_status(
            manager_ready=True,
            pid=request.pid,
            process_exit_code=exit_code,
        )
        return ContainerSandboxStatusResponse(
            ok=plan.ok,
            status=plan.status.value,
            exit_code=plan.exit_code,
            error_msg=plan.error_message,
        )

    def sandbox_stdout(
        self,
        request: ContainerSandboxStdoutRequest,
    ) -> ContainerSandboxStdoutResponse:
        manager = self._ready_manager_response(request.container_id)
        if isinstance(manager, str):
            return ContainerSandboxStdoutResponse(ok=False, error_msg=manager)
        try:
            return ContainerSandboxStdoutResponse(ok=True, stdout=manager.stdout(request.pid))
        except Exception as exc:
            return ContainerSandboxStdoutResponse(ok=False, error_msg=str(exc))
        finally:
            manager.cleanup()

    def sandbox_stderr(
        self,
        request: ContainerSandboxStderrRequest,
    ) -> ContainerSandboxStderrResponse:
        manager = self._ready_manager_response(request.container_id)
        if isinstance(manager, str):
            return ContainerSandboxStderrResponse(ok=False, error_msg=manager)
        try:
            return ContainerSandboxStderrResponse(ok=True, stderr=manager.stderr(request.pid))
        except Exception as exc:
            return ContainerSandboxStderrResponse(ok=False, error_msg=str(exc))
        finally:
            manager.cleanup()

    def sandbox_kill(self, request: ContainerSandboxKillRequest) -> ContainerSandboxKillResponse:
        manager = self._ready_manager_response(request.container_id)
        if isinstance(manager, str):
            return ContainerSandboxKillResponse(ok=False, error_msg=manager)
        try:
            manager.kill(request.pid)
        except Exception as exc:
            return ContainerSandboxKillResponse(ok=False, error_msg=str(exc))
        finally:
            manager.cleanup()
        return ContainerSandboxKillResponse(ok=True)

    def sandbox_list_processes(
        self,
        request: ContainerSandboxListProcessesRequest,
    ) -> ContainerSandboxListProcessesResponse:
        manager = self._ready_manager_response(request.container_id)
        if isinstance(manager, str):
            return ContainerSandboxListProcessesResponse(ok=False, error_msg=manager)
        try:
            processes = tuple(
                ContainerSandboxProcessInfo(pid=process.pid, command=process.command)
                for process in manager.list_processes()
            )
        except Exception as exc:
            return ContainerSandboxListProcessesResponse(ok=False, error_msg=str(exc))
        finally:
            manager.cleanup()
        return ContainerSandboxListProcessesResponse(ok=True, processes=processes)

    def sandbox_list_exposed_ports(
        self,
        request: ContainerSandboxListExposedPortsRequest,
    ) -> ContainerSandboxListExposedPortsResponse:
        instance = self._instance(request.container_id)
        if instance is None:
            return ContainerSandboxListExposedPortsResponse(
                ok=False,
                error_msg=CONTAINER_NOT_FOUND_MESSAGE,
            )
        plan = plan_sandbox_list_exposed_ports(instance.exposed_ports)
        return ContainerSandboxListExposedPortsResponse(
            ok=plan.ok,
            ports=tuple(plan.exposed_ports),
            error_msg=plan.error_message,
        )

    def sandbox_upload_file(
        self,
        request: ContainerSandboxUploadFileRequest,
    ) -> ContainerSandboxUploadFileResponse:
        try:
            instance = self._required_instance(request.container_id)
            filename = f"upload-{uuid4().hex}"
            staged = Path(WORKER_CONTAINER_UPLOADS_HOST_PATH) / instance.container_id / filename
            staged.parent.mkdir(parents=True, exist_ok=True)
            try:
                with staged.open("xb") as target:
                    target.write(request.data)
                return self._filesystem_response(
                    request.container_id,
                    SandboxFilesystemRequest(
                        operation=SandboxFileOperation.UploadFile,
                        path=request.container_path,
                        source=f"{WORKER_CONTAINER_UPLOADS_MOUNT_PATH}/{filename}",
                        mode=request.mode,
                    ),
                    ContainerSandboxUploadFileResponse,
                )
            finally:
                staged.unlink(missing_ok=True)
        except Exception as exc:
            return ContainerSandboxUploadFileResponse(ok=False, error_msg=str(exc))

    def sandbox_download_file(
        self,
        request: ContainerSandboxDownloadFileRequest,
    ) -> ContainerSandboxDownloadFileResponse:
        try:
            instance = self._required_instance(request.container_id)
            data, report = self._filesystem_output(
                instance,
                SandboxFilesystemRequest(
                    operation=SandboxFileOperation.DownloadFile,
                    path=request.container_path,
                    limit=request.max_bytes,
                    truncate=request.truncate,
                ),
            )
            try:
                written = SandboxDownloadReport.model_validate_json(report).bytes
            except ValidationError as exc:
                raise RuntimeError(
                    f"the sandbox supervisor did not report the size of {request.container_path}"
                ) from exc
            if written != len(data):
                raise RuntimeError(
                    f"received {len(data)} of the {written} bytes the sandbox "
                    f"supervisor wrote for {request.container_path}"
                )
            return ContainerSandboxDownloadFileResponse(ok=True, data=data)
        except SandboxFileOverLimitError as exc:
            return ContainerSandboxDownloadFileResponse(
                ok=False, error_msg=str(exc), over_limit=True
            )
        except Exception as exc:
            return ContainerSandboxDownloadFileResponse(ok=False, error_msg=str(exc))

    def sandbox_delete_file(
        self,
        request: ContainerSandboxDeleteFileRequest,
    ) -> ContainerSandboxDeleteFileResponse:
        return self._filesystem_response(
            request.container_id,
            SandboxFilesystemRequest(
                operation=SandboxFileOperation.DeleteFile, path=request.container_path
            ),
            ContainerSandboxDeleteFileResponse,
        )

    def sandbox_create_directory(
        self,
        request: ContainerSandboxCreateDirectoryRequest,
    ) -> ContainerSandboxCreateDirectoryResponse:
        return self._filesystem_response(
            request.container_id,
            SandboxFilesystemRequest(
                operation=SandboxFileOperation.CreateDirectory,
                path=request.container_path,
                mode=request.mode,
            ),
            ContainerSandboxCreateDirectoryResponse,
        )

    def sandbox_delete_directory(
        self,
        request: ContainerSandboxDeleteDirectoryRequest,
    ) -> ContainerSandboxDeleteDirectoryResponse:
        return self._filesystem_response(
            request.container_id,
            SandboxFilesystemRequest(
                operation=SandboxFileOperation.DeleteDirectory, path=request.container_path
            ),
            ContainerSandboxDeleteDirectoryResponse,
        )

    def sandbox_stat_file(
        self,
        request: ContainerSandboxStatFileRequest,
    ) -> ContainerSandboxStatFileResponse:
        return self._filesystem_response(
            request.container_id,
            SandboxFilesystemRequest(
                operation=SandboxFileOperation.StatFile, path=request.container_path
            ),
            ContainerSandboxStatFileResponse,
        )

    def sandbox_list_files(
        self,
        request: ContainerSandboxListFilesRequest,
    ) -> ContainerSandboxListFilesResponse:
        return self._filesystem_response(
            request.container_id,
            SandboxFilesystemRequest(
                operation=SandboxFileOperation.ListFiles,
                path=request.container_path,
                limit=request.limit,
            ),
            ContainerSandboxListFilesResponse,
        )

    def sandbox_replace_in_files(
        self,
        request: ContainerSandboxReplaceInFilesRequest,
    ) -> ContainerSandboxReplaceInFilesResponse:
        return self._filesystem_response(
            request.container_id,
            SandboxFilesystemRequest(
                operation=SandboxFileOperation.ReplaceInFiles,
                path=request.container_path,
                pattern=request.pattern,
                replacement=request.new_string,
            ),
            ContainerSandboxReplaceInFilesResponse,
        )

    def sandbox_find_in_files(
        self,
        request: ContainerSandboxFindInFilesRequest,
    ) -> ContainerSandboxFindInFilesResponse:
        return self._filesystem_response(
            request.container_id,
            SandboxFilesystemRequest(
                operation=SandboxFileOperation.FindInFiles,
                path=request.container_path,
                pattern=request.pattern,
            ),
            ContainerSandboxFindInFilesResponse,
        )

    def sandbox_expose_port(
        self,
        request: ContainerSandboxExposePortRequest,
    ) -> ContainerSandboxExposePortResponse:
        instance = self._instance(request.container_id)
        if instance is None:
            return ContainerSandboxExposePortResponse(
                ok=False, error_msg=CONTAINER_NOT_FOUND_MESSAGE
            )
        try:
            host_port = self.ports.allocate_port(instance, container_port=request.port)
            local_target = self.ports.local_target(
                instance,
                host_port=host_port,
                container_port=request.port,
            )
        except Exception as exc:
            return ContainerSandboxExposePortResponse(ok=False, error_msg=str(exc))
        plan = plan_sandbox_expose_port(
            SandboxExposePortRequest(
                container_id=request.container_id,
                port=request.port,
                existing_address_map=instance.address_map,
                existing_ports=instance.exposed_ports,
                host_port=host_port,
                local_target=local_target,
                route_context=instance.route_context,
            )
        )
        if not plan.ok:
            return ContainerSandboxExposePortResponse(ok=False, error_msg=plan.error_message)
        try:
            url = self.ports.publish_exposed_port(
                instance,
                port=request.port,
                local_target=plan.address_map[request.port],
                binding=plan.bind,
                address_map=plan.address_map,
                routes=plan.routes,
            )
        except Exception as exc:
            return ContainerSandboxExposePortResponse(ok=False, error_msg=str(exc))
        instance.address_map = plan.address_map
        instance.exposed_ports = plan.exposed_ports
        self.instances.save_container_instance(instance)
        return ContainerSandboxExposePortResponse(ok=True, url=url)

    def sandbox_unexpose_port(
        self,
        request: ContainerSandboxUnexposePortRequest,
    ) -> ContainerSandboxUnexposePortResponse:
        instance = self._instance(request.container_id)
        if instance is None:
            return ContainerSandboxUnexposePortResponse(
                ok=False,
                error_msg=CONTAINER_NOT_FOUND_MESSAGE,
            )
        local_target = instance.address_map.get(request.port, "")
        if not local_target:
            return ContainerSandboxUnexposePortResponse(ok=True)
        address_map = dict(instance.address_map)
        address_map.pop(request.port, None)
        try:
            self.ports.unpublish_exposed_port(
                instance,
                port=request.port,
                local_target=local_target,
                address_map=address_map,
            )
        except Exception as exc:
            return ContainerSandboxUnexposePortResponse(ok=False, error_msg=str(exc))
        instance.address_map = address_map
        instance.exposed_ports = [port for port in instance.exposed_ports if port != request.port]
        self.instances.save_container_instance(instance)
        return ContainerSandboxUnexposePortResponse(ok=True)

    def sandbox_update_network_permissions(
        self,
        request: ContainerSandboxUpdateNetworkPermissionsRequest,
    ) -> ContainerSandboxUpdateNetworkPermissionsResponse:
        instance = self._instance(request.container_id)
        if instance is None:
            return ContainerSandboxUpdateNetworkPermissionsResponse(
                ok=False,
                error_msg=CONTAINER_NOT_FOUND_MESSAGE,
            )
        if self.network_policy is None:
            return ContainerSandboxUpdateNetworkPermissionsResponse(
                ok=False,
                error_msg="container network policy updater unavailable",
            )
        allow_list = list(request.allow_list)
        try:
            self.network_policy.update_network_permissions(
                request.container_id,
                block_network=request.block_network,
                allow_list=allow_list,
            )
        except Exception as exc:
            return ContainerSandboxUpdateNetworkPermissionsResponse(
                ok=False,
                error_msg=str(exc),
            )
        instance.network_blocked = request.block_network
        instance.network_allow_list = allow_list
        self.instances.save_container_instance(instance)
        return ContainerSandboxUpdateNetworkPermissionsResponse(ok=True)

    def sync_workspace(
        self,
        request: WorkspaceSyncBatch,
    ) -> SyncContainerWorkspaceResponse:
        try:
            instance = self._required_instance(request.manifest.container_id)
            applied = apply_workspace_batch(instance.workspace_root, request)
            return SyncContainerWorkspaceResponse(applied=applied)
        except (ValueError, OSError) as exc:
            return SyncContainerWorkspaceResponse(ok=False, error_msg=str(exc))

    def _filesystem_response[Response: ContractModel](
        self,
        container_id: str,
        request: SandboxFilesystemRequest,
        response_type: type[Response],
    ) -> Response:
        try:
            instance = self._required_instance(container_id)
            stdout, _ = self._filesystem_output(instance, request)
            return response_type.model_validate_json(stdout)
        except Exception as exc:
            return response_type.model_validate({"ok": False, "error_msg": str(exc)})

    def _filesystem_output(
        self,
        instance: WorkerContainerServiceInstance,
        request: SandboxFilesystemRequest,
    ) -> tuple[bytes, bytes]:
        request.path = resolve_sandbox_container_path(request.path, cwd=instance.cwd)
        manager = self._ready_process_manager(instance)
        if isinstance(manager, str):
            raise RuntimeError(manager)
        try:
            output = manager.run_filesystem(request.model_dump_json())
        finally:
            manager.cleanup()
        stderr = output.stderr.decode("utf-8", errors="replace").strip()
        if output.exit_code == SANDBOX_FILESYSTEM_OVER_LIMIT_EXIT:
            raise SandboxFileOverLimitError(stderr)
        if output.exit_code != 0:
            raise RuntimeError(stderr or f"sandbox file operation exited {output.exit_code}")
        return output.stdout, output.stderr

    def _append_process_log(
        self,
        instance: WorkerContainerServiceInstance,
        event: SandboxProcessEvent,
        argv: list[str],
        cwd: str,
    ) -> str:
        if not event.data:
            return ""
        ack = plan_sandbox_process_log_ack(
            container_id=instance.container_id,
            stub_id=instance.stub_id,
            workspace_id=instance.workspace_id,
            app_id=instance.app_id,
            worker_id=instance.worker_id,
            stream=event.stream,
            seq=event.seq,
            pid=event.pid,
            data=event.data,
            process_args=argv,
            process_cwd=cwd,
        )
        if self.logs is not None:
            try:
                self.logs.append_sandbox_process_log(ack.entry)
            except Exception as exc:
                return str(exc)
        instance.log_messages.append(ack.entry.line)
        return ""

    def _consume_sandbox_process_events(
        self,
        manager: WorkerSandboxProcessManager,
        events: Iterable[SandboxProcessEvent],
        instance: WorkerContainerServiceInstance,
        argv: list[str],
        cwd: str,
    ) -> None:
        try:
            for event in events:
                if event.event_type is not SandboxProcessEventType.Chunk:
                    continue
                error = self._append_process_log(instance, event, argv, cwd)
                manager.ack(event.pid, event.seq, ok=not error)
                if error:
                    instance.sandbox_process_stderr[event.pid] = (
                        instance.sandbox_process_stderr.get(event.pid, "")
                        + f"sandbox process log persistence failed: {error}\n"
                    )
                    return
        finally:
            self.instances.save_container_instance(instance)
            manager.cleanup()

    def _ready_manager_response(
        self,
        container_id: str,
    ) -> WorkerSandboxProcessManager | str:
        instance = self._instance(container_id)
        if instance is None:
            return CONTAINER_NOT_FOUND_MESSAGE
        return self._ready_process_manager(instance)

    def _ready_process_manager(
        self,
        instance: WorkerContainerServiceInstance,
    ) -> WorkerSandboxProcessManager | str:
        manager = self._process_manager(instance)
        if manager is None:
            return "sandbox process manager is not configured"
        if instance.sandbox_process_manager_ready:
            if instance.docker_enabled:
                if self.sandbox_docker is None:
                    manager.cleanup()
                    return "Docker-enabled sandbox lifecycle is not configured"
                try:
                    self.sandbox_docker.ensure_ready(instance)
                except Exception as exc:
                    manager.cleanup()
                    return str(exc)
            return manager
        try:
            manager_ready = manager.ready()
        except Exception as exc:
            manager.cleanup()
            return str(exc)
        if not manager_ready:
            manager.cleanup()
            return SANDBOX_PROCESS_MANAGER_NOT_READY_MESSAGE
        instance.sandbox_process_manager_ready = True
        self.instances.save_container_instance(instance)
        if instance.docker_enabled:
            if self.sandbox_docker is None:
                manager.cleanup()
                return "Docker-enabled sandbox lifecycle is not configured"
            try:
                self.sandbox_docker.ensure_ready(instance)
            except Exception as exc:
                manager.cleanup()
                return str(exc)
        return manager

    def _process_manager(
        self,
        instance: WorkerContainerServiceInstance,
    ) -> WorkerSandboxProcessManager | None:
        if self.process_managers is None:
            return None
        return self.process_managers.create_process_manager(instance)

    def _instance(self, container_id: str) -> WorkerContainerServiceInstance | None:
        return self.instances.get_container_instance(container_id)

    def _required_instance(self, container_id: str) -> WorkerContainerServiceInstance:
        instance = self._instance(container_id)
        if instance is None:
            raise ValueError(CONTAINER_NOT_FOUND_MESSAGE)
        return instance


def _stream_build_request_logs(
    container_id: str,
    instances: WorkerContainerInstanceStore,
    *,
    poll_interval_seconds: float = 0.25,
) -> Iterable[ContainerLogEntry]:
    cursor = 0
    while True:
        instance = instances.get_container_instance(container_id)
        if instance is None:
            return
        messages = list(instance.log_messages)
        while cursor < len(messages):
            yield ContainerLogEntry(msg=messages[cursor])
            cursor += 1
        if instance.status and instance.status != "running":
            return
        time.sleep(poll_interval_seconds)


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _container_runtime_missing(error: Exception) -> bool:
    message = str(error).lower()
    return (
        "container does not exist" in message
        or "container not found" in message
        or "no such container" in message
    )
