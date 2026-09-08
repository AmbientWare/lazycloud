from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import Thread
from uuid import uuid4

from shared.deployments import StubKind

from worker.container_client.models import (
    ContainerArchiveRequest,
    ContainerArchiveResponse,
    ContainerCheckpointRequest,
    ContainerCheckpointResponse,
    ContainerExecRequest,
    ContainerExecResponse,
    ContainerFileSearchMatch,
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
    ContainerSandboxFileInfo,
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
    LocalSandboxPortPublisher,
    WorkerContainerArchiveCreator,
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
    WorkspaceSyncOperation,
    plan_container_exec,
    plan_container_kill,
    plan_sandbox_exec,
    plan_workspace_sync,
)
from worker.runtime_config import OciRuntimeName
from worker.sandbox_server import (
    SandboxExposePortRequest,
    SandboxFileAccessMode,
    SandboxFileOperation,
    SandboxFileOperationPlan,
    SandboxLogStream,
    plan_sandbox_expose_port,
    plan_sandbox_file_operation,
    plan_sandbox_list_exposed_ports,
    plan_sandbox_process_log_ack,
    plan_sandbox_status,
    plan_sandbox_upload_file,
)


@dataclass(slots=True)
class WorkerContainerService:
    instances: WorkerContainerInstanceStore
    process_managers: WorkerSandboxProcessManagerFactory | None = None
    sandbox_docker: WorkerSandboxDockerLifecycle | None = None
    runtime: WorkerContainerRuntimeController | None = None
    logs: WorkerSandboxLogSink | None = None
    checkpoints: WorkerContainerCheckpointCreator | None = None
    archives: WorkerContainerArchiveCreator | None = None
    network_policy: WorkerSandboxNetworkPolicyUpdater | None = None
    ports: WorkerSandboxPortPublisher = field(default_factory=LocalSandboxPortPublisher)

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
        instance = self._instance(request.container_id)
        if instance is None:
            return ContainerSandboxUploadFileResponse(
                ok=False, error_msg=CONTAINER_NOT_FOUND_MESSAGE
            )
        plan = plan_sandbox_upload_file(
            container_id=request.container_id,
            container_path=request.container_path,
            root_path=instance.root_path,
            mounts=instance.mounts,
            cwd=instance.cwd,
            runtime=instance.runtime,
            mode=request.mode,
            data_size_bytes=len(request.data),
            upload_file_name=f"upload_{uuid4().hex}",
        )
        try:
            if plan.access_mode is SandboxFileAccessMode.SandboxedRuntimeStagedUpload:
                if self.runtime is None:
                    return ContainerSandboxUploadFileResponse(
                        ok=False,
                        error_msg="container runtime is required for staged sandbox uploads",
                    )
                temp_path = Path(plan.temp_host_path)
                temp_path.parent.mkdir(parents=True, exist_ok=True)
                temp_path.write_bytes(request.data)
                temp_path.chmod(request.mode)
                try:
                    result = self.runtime.exec_container(
                        request.container_id,
                        argv=["sh", "-c", plan.command],
                        env=instance.env,
                        cwd=instance.cwd,
                    )
                    if not result.ok:
                        return ContainerSandboxUploadFileResponse(
                            ok=False,
                            error_msg=result.error_msg or "staged upload move failed",
                        )
                finally:
                    temp_path.unlink(missing_ok=True)
                return ContainerSandboxUploadFileResponse(ok=True)
            host_path = Path(plan.host_path)
            host_path.parent.mkdir(parents=True, exist_ok=True)
            host_path.write_bytes(request.data)
            host_path.chmod(request.mode)
            return ContainerSandboxUploadFileResponse(ok=True)
        except Exception as exc:
            return ContainerSandboxUploadFileResponse(ok=False, error_msg=str(exc))

    def sandbox_download_file(
        self,
        request: ContainerSandboxDownloadFileRequest,
    ) -> ContainerSandboxDownloadFileResponse:
        try:
            instance = self._required_instance(request.container_id)
            plan = self._file_plan(
                instance,
                request.container_path,
                SandboxFileOperation.DownloadFile,
            )
            if instance.runtime is OciRuntimeName.Runsc:
                stdout, stderr, exit_code = self._sandbox_control_exec(
                    instance,
                    ["cat", plan.container_path],
                )
                if exit_code != 0:
                    detail = stderr.decode("utf-8", errors="replace").strip()
                    return ContainerSandboxDownloadFileResponse(
                        ok=False,
                        error_msg=detail or f"sandbox file read exited {exit_code}",
                    )
                return ContainerSandboxDownloadFileResponse(ok=True, data=stdout)
            return ContainerSandboxDownloadFileResponse(
                ok=True,
                data=Path(plan.host_path).read_bytes(),
            )
        except Exception as exc:
            return ContainerSandboxDownloadFileResponse(ok=False, error_msg=str(exc))

    def sandbox_delete_file(
        self,
        request: ContainerSandboxDeleteFileRequest,
    ) -> ContainerSandboxDeleteFileResponse:
        try:
            _remove_path(
                self._file_path(
                    request.container_id,
                    request.container_path,
                    SandboxFileOperation.DeleteFile,
                )
            )
            return ContainerSandboxDeleteFileResponse(ok=True)
        except Exception as exc:
            return ContainerSandboxDeleteFileResponse(ok=False, error_msg=str(exc))

    def sandbox_create_directory(
        self,
        request: ContainerSandboxCreateDirectoryRequest,
    ) -> ContainerSandboxCreateDirectoryResponse:
        try:
            path = self._file_path(
                request.container_id,
                request.container_path,
                SandboxFileOperation.CreateDirectory,
                mode=request.mode,
            )
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(request.mode)
            return ContainerSandboxCreateDirectoryResponse(ok=True)
        except Exception as exc:
            return ContainerSandboxCreateDirectoryResponse(ok=False, error_msg=str(exc))

    def sandbox_delete_directory(
        self,
        request: ContainerSandboxDeleteDirectoryRequest,
    ) -> ContainerSandboxDeleteDirectoryResponse:
        try:
            path = self._file_path(
                request.container_id,
                request.container_path,
                SandboxFileOperation.DeleteDirectory,
            )
            if path.is_dir():
                shutil.rmtree(path)
            return ContainerSandboxDeleteDirectoryResponse(ok=True)
        except Exception as exc:
            return ContainerSandboxDeleteDirectoryResponse(ok=False, error_msg=str(exc))

    def sandbox_stat_file(
        self,
        request: ContainerSandboxStatFileRequest,
    ) -> ContainerSandboxStatFileResponse:
        try:
            return ContainerSandboxStatFileResponse(
                ok=True,
                file_info=_container_file_info(
                    self._file_path(
                        request.container_id,
                        request.container_path,
                        SandboxFileOperation.StatFile,
                    )
                ),
            )
        except Exception as exc:
            return ContainerSandboxStatFileResponse(ok=False, error_msg=str(exc))

    def sandbox_list_files(
        self,
        request: ContainerSandboxListFilesRequest,
    ) -> ContainerSandboxListFilesResponse:
        try:
            path = self._file_path(
                request.container_id,
                request.container_path,
                SandboxFileOperation.ListFiles,
            )
            if not path.exists():
                return ContainerSandboxListFilesResponse(ok=True)
            children = (path,) if path.is_file() else tuple(sorted(path.iterdir()))
            return ContainerSandboxListFilesResponse(
                ok=True,
                files=tuple(_container_file_info(child) for child in children),
            )
        except Exception as exc:
            return ContainerSandboxListFilesResponse(ok=False, error_msg=str(exc))

    def sandbox_replace_in_files(
        self,
        request: ContainerSandboxReplaceInFilesRequest,
    ) -> ContainerSandboxReplaceInFilesResponse:
        try:
            target = self._file_path(
                request.container_id,
                request.container_path,
                SandboxFileOperation.ReplaceInFiles,
            )
            for file_path in _iter_text_files(target):
                content = file_path.read_text(encoding="utf-8")
                if request.pattern in content:
                    file_path.write_text(
                        content.replace(request.pattern, request.new_string),
                        encoding="utf-8",
                    )
            return ContainerSandboxReplaceInFilesResponse(ok=True)
        except Exception as exc:
            return ContainerSandboxReplaceInFilesResponse(ok=False, error_msg=str(exc))

    def sandbox_find_in_files(
        self,
        request: ContainerSandboxFindInFilesRequest,
    ) -> ContainerSandboxFindInFilesResponse:
        try:
            instance = self._required_instance(request.container_id)
            target = self._file_path(
                request.container_id,
                request.container_path,
                SandboxFileOperation.FindInFiles,
            )
            root = Path(instance.root_path)
            return ContainerSandboxFindInFilesResponse(
                ok=True,
                results=tuple(
                    _search_match(file_path, root, request.pattern)
                    for file_path in _iter_text_files(target)
                    if request.pattern in file_path.read_text(encoding="utf-8")
                ),
            )
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
                agent_worker=instance.agent_worker,
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

    def _file_path(
        self,
        container_id: str,
        container_path: str,
        operation: SandboxFileOperation,
        *,
        mode: int = 0o644,
    ) -> Path:
        instance = self._required_instance(container_id)
        return Path(self._file_plan(instance, container_path, operation, mode=mode).host_path)

    @staticmethod
    def _file_plan(
        instance: WorkerContainerServiceInstance,
        container_path: str,
        operation: SandboxFileOperation,
        *,
        mode: int = 0o644,
    ) -> SandboxFileOperationPlan:
        return plan_sandbox_file_operation(
            operation,
            container_id=instance.container_id,
            container_path=container_path,
            root_path=instance.root_path,
            mounts=instance.mounts,
            cwd=instance.cwd,
            mode=mode,
        )

    def _sandbox_control_exec(
        self,
        instance: WorkerContainerServiceInstance,
        argv: list[str],
    ) -> tuple[bytes, bytes, int]:
        manager = self._ready_process_manager(instance)
        if isinstance(manager, str):
            raise RuntimeError(manager)
        stdout = bytearray()
        stderr = bytearray()
        exit_code: int | None = None
        try:
            for event in manager.stream_exec(argv, cwd=instance.cwd, env=instance.env):
                if event.event_type is SandboxProcessEventType.Chunk:
                    if event.stream is SandboxLogStream.Stderr:
                        stderr.extend(event.data)
                    else:
                        stdout.extend(event.data)
                    manager.ack(event.pid, event.seq, ok=True)
                elif event.event_type is SandboxProcessEventType.Exited:
                    exit_code = event.exit_code
        finally:
            manager.cleanup()
        if exit_code is None:
            raise RuntimeError("sandbox control command ended without an exit event")
        return bytes(stdout), bytes(stderr), exit_code

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


def _container_file_info(path: Path) -> ContainerSandboxFileInfo:
    stat = path.stat()
    return ContainerSandboxFileInfo(
        name=path.name,
        mode=stat.st_mode,
        size=stat.st_size,
        mod_time=int(stat.st_mtime),
        owner=str(getattr(stat, "st_uid", "")),
        group=str(getattr(stat, "st_gid", "")),
        is_dir=path.is_dir(),
        permissions=stat.st_mode & 0o777,
    )


def _iter_text_files(path: Path) -> Iterable[Path]:
    candidates = (path,) if path.is_file() else tuple(sorted(path.rglob("*")))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        yield candidate


def _search_match(path: Path, root: Path, pattern: str) -> ContainerFileSearchMatch:
    content = path.read_text(encoding="utf-8")
    for line_number, line in enumerate(content.splitlines(), start=1):
        column = line.find(pattern)
        if column != -1:
            return ContainerFileSearchMatch(
                path=_display_path(path, root),
                text=pattern,
                line=line_number,
                column=column + 1,
            )
    return ContainerFileSearchMatch(path=_display_path(path, root), text=pattern)


def _display_path(path: Path, root: Path) -> str:
    try:
        return os.fspath(path.relative_to(root))
    except ValueError:
        return os.fspath(path)


def _container_runtime_missing(error: Exception) -> bool:
    message = str(error).lower()
    return (
        "container does not exist" in message
        or "container not found" in message
        or "no such container" in message
    )
