from __future__ import annotations

import shutil
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import Thread

from shared.deployments import StubKind

from worker.container_client.models import (
    ContainerArchiveRequest,
    ContainerArchiveResponse,
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
    SyncContainerWorkspaceRequest,
    SyncContainerWorkspaceResponse,
)
from worker.container_service.models import (
    CONTAINER_NOT_FOUND_MESSAGE,
    SANDBOX_PROCESS_MANAGER_NOT_READY_MESSAGE,
    SandboxProcessEvent,
    SandboxProcessEventType,
    WorkerContainerServiceInstance,
)
from worker.container_service.protocols import (
    BridgeSandboxPortPublisher,
    WorkerContainerArchiveCreator,
    WorkerContainerCheckpointCreator,
    WorkerContainerInstanceStore,
    WorkerContainerRuntimeController,
    WorkerSandboxControlManager,
    WorkerSandboxControlManagerFactory,
    WorkerSandboxDockerLifecycle,
    WorkerSandboxLogSink,
    WorkerSandboxNetworkPolicyUpdater,
    WorkerSandboxPortPublisher,
    WorkerSandboxProcessManager,
)
from worker.execution import (
    WorkspaceSyncOperation,
    plan_container_exec,
    plan_container_kill,
    plan_sandbox_exec,
    plan_workspace_sync,
)
from worker.sandbox_server import (
    SandboxExposePortRequest,
    SandboxFileOperation,
    SandboxFileRequest,
    SandboxFileResult,
    plan_sandbox_expose_port,
    plan_sandbox_list_exposed_ports,
    plan_sandbox_process_log_ack,
    plan_sandbox_status,
)


@dataclass(slots=True)
class WorkerContainerService:
    instances: WorkerContainerInstanceStore
    process_managers: WorkerSandboxControlManagerFactory | None = None
    sandbox_docker: WorkerSandboxDockerLifecycle | None = None
    runtime: WorkerContainerRuntimeController | None = None
    logs: WorkerSandboxLogSink | None = None
    checkpoints: WorkerContainerCheckpointCreator | None = None
    archives: WorkerContainerArchiveCreator | None = None
    network_policy: WorkerSandboxNetworkPolicyUpdater | None = None
    ports: WorkerSandboxPortPublisher = field(default_factory=BridgeSandboxPortPublisher)

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

    def container_archive(
        self,
        request: ContainerArchiveRequest,
    ) -> tuple[ContainerArchiveResponse, ...]:
        instance = self._instance(request.container_id)
        if instance is None:
            return (
                ContainerArchiveResponse(
                    done=True,
                    success=False,
                    error_msg=CONTAINER_NOT_FOUND_MESSAGE,
                ),
            )
        if self.archives is None:
            return (
                ContainerArchiveResponse(
                    done=True,
                    success=False,
                    error_msg="archive creator is not configured",
                ),
            )
        try:
            return tuple(
                self.archives.archive_container(
                    instance,
                    image_id=request.image_id,
                )
            )
        except Exception as exc:
            return (ContainerArchiveResponse(done=True, success=False, error_msg=str(exc)),)

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
            self._sandbox_file_operation(
                request.container_id,
                SandboxFileRequest(
                    operation=SandboxFileOperation.UploadFile,
                    container_path=request.container_path,
                    mode=request.mode,
                    data=request.data,
                ),
            )
            return ContainerSandboxUploadFileResponse(ok=True)
        except Exception as exc:
            return ContainerSandboxUploadFileResponse(ok=False, error_msg=str(exc))

    def sandbox_download_file(
        self,
        request: ContainerSandboxDownloadFileRequest,
    ) -> ContainerSandboxDownloadFileResponse:
        try:
            result = self._sandbox_file_operation(
                request.container_id,
                SandboxFileRequest(
                    operation=SandboxFileOperation.DownloadFile,
                    container_path=request.container_path,
                ),
            )
            return ContainerSandboxDownloadFileResponse(ok=True, data=result.data)
        except Exception as exc:
            return ContainerSandboxDownloadFileResponse(ok=False, error_msg=str(exc))

    def sandbox_delete_file(
        self,
        request: ContainerSandboxDeleteFileRequest,
    ) -> ContainerSandboxDeleteFileResponse:
        try:
            self._sandbox_file_operation(
                request.container_id,
                SandboxFileRequest(
                    operation=SandboxFileOperation.DeleteFile,
                    container_path=request.container_path,
                ),
            )
            return ContainerSandboxDeleteFileResponse(ok=True)
        except Exception as exc:
            return ContainerSandboxDeleteFileResponse(ok=False, error_msg=str(exc))

    def sandbox_create_directory(
        self,
        request: ContainerSandboxCreateDirectoryRequest,
    ) -> ContainerSandboxCreateDirectoryResponse:
        try:
            self._sandbox_file_operation(
                request.container_id,
                SandboxFileRequest(
                    operation=SandboxFileOperation.CreateDirectory,
                    container_path=request.container_path,
                    mode=request.mode,
                ),
            )
            return ContainerSandboxCreateDirectoryResponse(ok=True)
        except Exception as exc:
            return ContainerSandboxCreateDirectoryResponse(ok=False, error_msg=str(exc))

    def sandbox_delete_directory(
        self,
        request: ContainerSandboxDeleteDirectoryRequest,
    ) -> ContainerSandboxDeleteDirectoryResponse:
        try:
            self._sandbox_file_operation(
                request.container_id,
                SandboxFileRequest(
                    operation=SandboxFileOperation.DeleteDirectory,
                    container_path=request.container_path,
                ),
            )
            return ContainerSandboxDeleteDirectoryResponse(ok=True)
        except Exception as exc:
            return ContainerSandboxDeleteDirectoryResponse(ok=False, error_msg=str(exc))

    def sandbox_stat_file(
        self,
        request: ContainerSandboxStatFileRequest,
    ) -> ContainerSandboxStatFileResponse:
        try:
            result = self._sandbox_file_operation(
                request.container_id,
                SandboxFileRequest(
                    operation=SandboxFileOperation.StatFile,
                    container_path=request.container_path,
                ),
            )
            if result.file_info is None:
                raise RuntimeError("sandbox file stat returned no metadata")
            return ContainerSandboxStatFileResponse(ok=True, file_info=result.file_info)
        except Exception as exc:
            return ContainerSandboxStatFileResponse(ok=False, error_msg=str(exc))

    def sandbox_list_files(
        self,
        request: ContainerSandboxListFilesRequest,
    ) -> ContainerSandboxListFilesResponse:
        try:
            result = self._sandbox_file_operation(
                request.container_id,
                SandboxFileRequest(
                    operation=SandboxFileOperation.ListFiles,
                    container_path=request.container_path,
                ),
            )
            return ContainerSandboxListFilesResponse(ok=True, files=result.files)
        except Exception as exc:
            return ContainerSandboxListFilesResponse(ok=False, error_msg=str(exc))

    def sandbox_replace_in_files(
        self,
        request: ContainerSandboxReplaceInFilesRequest,
    ) -> ContainerSandboxReplaceInFilesResponse:
        try:
            self._sandbox_file_operation(
                request.container_id,
                SandboxFileRequest(
                    operation=SandboxFileOperation.ReplaceInFiles,
                    container_path=request.container_path,
                    pattern=request.pattern,
                    new_string=request.new_string,
                ),
            )
            return ContainerSandboxReplaceInFilesResponse(ok=True)
        except Exception as exc:
            return ContainerSandboxReplaceInFilesResponse(ok=False, error_msg=str(exc))

    def sandbox_find_in_files(
        self,
        request: ContainerSandboxFindInFilesRequest,
    ) -> ContainerSandboxFindInFilesResponse:
        try:
            result = self._sandbox_file_operation(
                request.container_id,
                SandboxFileRequest(
                    operation=SandboxFileOperation.FindInFiles,
                    container_path=request.container_path,
                    pattern=request.pattern,
                ),
            )
            return ContainerSandboxFindInFilesResponse(ok=True, results=result.matches)
        except Exception as exc:
            return ContainerSandboxFindInFilesResponse(ok=False, error_msg=str(exc))

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
        request: SyncContainerWorkspaceRequest,
    ) -> SyncContainerWorkspaceResponse:
        try:
            instance = self._required_instance(request.container_id)
            plan = plan_workspace_sync(
                request.container_id,
                workspace_root=instance.workspace_root,
                operation=WorkspaceSyncOperation(request.operation.value),
                path=request.path,
                new_path=request.new_path or None,
                is_dir=request.operation.value == WorkspaceSyncOperation.Write.value
                and request.metadata.get("is_dir") is True,
                data_size_bytes=len(request.data),
            )
            target = Path(plan.target_path)
            if plan.operation is WorkspaceSyncOperation.Delete:
                _remove_path(target)
            elif plan.operation is WorkspaceSyncOperation.Write:
                if plan.is_dir:
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(request.data)
            elif plan.operation is WorkspaceSyncOperation.Move:
                if plan.new_path is None:
                    return SyncContainerWorkspaceResponse(
                        ok=False,
                        error_msg="new_path is required for move operations",
                    )
                new_path = Path(plan.new_path)
                new_path.parent.mkdir(parents=True, exist_ok=True)
                target.rename(new_path)
            return SyncContainerWorkspaceResponse(ok=True, path=plan.target_path)
        except Exception as exc:
            return SyncContainerWorkspaceResponse(ok=False, error_msg=str(exc))

    def _sandbox_file_operation(
        self,
        container_id: str,
        request: SandboxFileRequest,
    ) -> SandboxFileResult:
        instance = self._required_instance(container_id)
        manager = self._ready_process_manager(instance)
        if isinstance(manager, str):
            raise RuntimeError(manager)
        try:
            return manager.file_operation(request, cwd=instance.cwd)
        finally:
            manager.cleanup()

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
    ) -> WorkerSandboxControlManager | str:
        instance = self._instance(container_id)
        if instance is None:
            return CONTAINER_NOT_FOUND_MESSAGE
        return self._ready_process_manager(instance)

    def _ready_process_manager(
        self,
        instance: WorkerContainerServiceInstance,
    ) -> WorkerSandboxControlManager | str:
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
    ) -> WorkerSandboxControlManager | None:
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
