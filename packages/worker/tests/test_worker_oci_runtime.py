from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from foundation.process import (
    ManagedCommand,
    ManagedCommandResult,
    ManagedCommandState,
    ProcessOutputSink,
    ProcessResult,
)
from pydantic import JsonValue, TypeAdapter
from shared.agent_connections import AGENT_TUNNEL_CONTROL_PORT
from worker.container_execution import (
    ContainerExecutionContext,
    ContainerMountSetupResult,
)
from worker.container_rootfs import (
    ContainerRootfsSetupResult,
    ContainerRootfsStatus,
)
from worker.events import ContainerRequestContext
from worker.execution import (
    ContainerEnvironmentRequest,
    GatewayEndpointSettings,
    GatewayServiceSettings,
    OciDevice,
    build_container_environment,
)
from worker.gpu import ContainerGpuAssignmentResult
from worker.image_lifecycle import ImageRuntimeConfig
from worker.network_backend import AgentBridgeNetworkConfig
from worker.oci_runtime import (
    OciRuntimeCommandController,
    OciRuntimeCommandTimeout,
    OciRuntimeCommandTimeouts,
    OciRuntimeSpecBuilder,
)
from worker.runtime_config import (
    OciRuntimeName,
    RuntimeBinaryConfig,
    build_base_oci_config,
    prepare_oci_spec_for_runtime,
)

_STRING_LIST: TypeAdapter[list[str]] = TypeAdapter(list[str])


def test_image_environment_cannot_override_workload_or_platform_identity() -> None:
    environment = build_container_environment(
        ContainerEnvironmentRequest(
            container_id="container",
            pod_address="127.0.0.1",
            workspace_id="owner-workspace",
            image_env=[
                "IMAGE_VALUE=present",
                "SHARED_VALUE=image",
                "PATH=/opt/lazycloud/venv/bin:/usr/bin",
                "WORKSPACE_ID=image-workspace",
                "GATEWAY_HTTP_URL=http://image-gateway",
                "GATEWAY_TOKEN=image-token",
            ],
            request_env=[
                "SHARED_VALUE=workload",
                "GATEWAY_TOKEN=container-token",
            ],
        ),
        GatewayServiceSettings(http=GatewayEndpointSettings(host="127.0.0.1", port=9000)),
    ).env_map

    assert environment["IMAGE_VALUE"] == "present"
    assert environment["SHARED_VALUE"] == "workload"
    assert environment["PATH"] == "/opt/lazycloud/venv/bin:/usr/bin"
    assert environment["WORKSPACE_ID"] == "owner-workspace"
    assert environment["GATEWAY_HTTP_URL"] == "http://127.0.0.1:9000"
    assert environment["GATEWAY_TOKEN"] == "container-token"
    unauthenticated = build_container_environment(
        ContainerEnvironmentRequest(
            container_id="container",
            pod_address="127.0.0.1",
            image_env=["GATEWAY_TOKEN=image-token"],
        ),
        GatewayServiceSettings(http=GatewayEndpointSettings(host="127.0.0.1", port=9000)),
    ).env_map
    assert "GATEWAY_TOKEN" not in unauthenticated


def test_oci_runtime_reaches_a_container_persisted_before_the_move_to_gvisor() -> None:
    """A container recorded as runc still has to be reachable.

    The worker advertises only runsc, so looking the persisted name up verbatim
    raised "worker did not advertise this runtime capability" and left a running
    container with no route to stop or inspect it.
    """
    runner = _Runner()
    runtime_by_container = {
        "ctr-runc": OciRuntimeName.Runc,
        "ctr-runsc": OciRuntimeName.Runsc,
    }
    controller = OciRuntimeCommandController(
        run_command=runner.run,
        runtime_configs={
            OciRuntimeName.Runsc: RuntimeBinaryConfig(
                runtime=OciRuntimeName.Runsc,
                runsc_path="/usr/bin/runsc",
            ),
        },
        container_runtime=runtime_by_container.get,
    )

    assert controller.status("ctr-runsc") == "running"
    assert runner.commands[-1][0] == "/usr/bin/runsc"
    assert controller.status("ctr-runc") == "running"
    assert runner.commands[-1][0] == "/usr/bin/runsc"


def test_oci_runtime_treats_runsc_missing_state_as_stopped() -> None:
    runner = _MissingRunscStateRunner()
    controller = OciRuntimeCommandController(
        run_command=runner.run,
        runtime_configs={
            OciRuntimeName.Runsc: RuntimeBinaryConfig(
                runtime=OciRuntimeName.Runsc,
                runsc_path="/usr/bin/runsc",
            )
        },
        container_runtime=lambda _container_id: OciRuntimeName.Runsc,
    )

    assert controller.status("ctr-1") == "stopped"


def _context(tmp_path: Path) -> ContainerExecutionContext:
    _ = tmp_path
    return ContainerExecutionContext(
        request=ContainerRequestContext(
            container_id="ctr-1",
            image_id="image-1",
            workspace_id="workspace-1",
            stub_id="stub-1",
            env=["APP_ENV=prod"],
            cpu_millicores=1000,
            memory_mib=512,
        ),
        runtime=OciRuntimeName.Runsc,
    )


def _prepared_rootfs(tmp_path: Path, container_id: str = "ctr-1") -> ContainerRootfsSetupResult:
    merged = tmp_path / "container-rootfs" / container_id / "merged"
    merged.mkdir(parents=True, exist_ok=True)
    return ContainerRootfsSetupResult(
        container_id=container_id,
        status=ContainerRootfsStatus.Mounted,
        root_path=str(merged),
        upper_path=str(tmp_path / "container-rootfs" / container_id / "upper"),
    )


@dataclass(slots=True)
class _Runner:
    commands: list[list[str]] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)
    killed: bool = False

    def run(
        self,
        args: list[str],
        *,
        timeout_seconds: float,
        termination_timeout_seconds: float,
        max_output_chars: int,
    ) -> ProcessResult:
        del termination_timeout_seconds, max_output_chars
        self.commands.append(list(args))
        self.timeouts.append(timeout_seconds)
        if "kill" in args:
            self.killed = True
        if "state" in args:
            return ProcessResult(
                args=args,
                exit_code=0,
                stdout=(
                    '{"id":"ctr-1","pid":0,"status":"stopped"}'
                    if self.killed
                    else '{"id":"ctr-1","pid":8765,"status":"running"}'
                ),
                stderr="",
            )
        if "exec" in args:
            return ProcessResult(args=args, exit_code=0, stdout="exec-ok\n", stderr="")
        return ProcessResult(args=args, exit_code=0, stdout="", stderr="")


@dataclass(slots=True)
class _MissingRunscStateRunner(_Runner):
    def run(
        self,
        args: list[str],
        *,
        timeout_seconds: float,
        termination_timeout_seconds: float,
        max_output_chars: int,
    ) -> ProcessResult:
        del termination_timeout_seconds, max_output_chars
        self.commands.append(list(args))
        self.timeouts.append(timeout_seconds)
        return ProcessResult(
            args=args,
            exit_code=128,
            stdout="",
            stderr="loading container: file does not exist",
        )


@dataclass(slots=True)
class _Starter:
    commands: list[list[str]] = field(default_factory=list)
    started: list[int] = field(default_factory=list)
    managed: list[_ManagedCommand] = field(default_factory=list)
    output_sinks: list[ProcessOutputSink | None] = field(default_factory=list)

    def start(
        self,
        args: list[str],
        *,
        output_sink: ProcessOutputSink | None = None,
    ) -> ManagedCommand:
        self.commands.append(list(args))
        self.output_sinks.append(output_sink)
        command = _ManagedCommand(args=args)
        self.managed.append(command)
        return command


@dataclass(slots=True)
class _ManagedCommand(ManagedCommand):
    args: list[str]
    _pid: int = 4321
    terminated: bool = False
    completed: ManagedCommandResult | None = None
    complete_after_polls: int = 0
    poll_calls: int = 0

    @property
    def pid(self) -> int:
        return self._pid

    def wait(self, *, timeout_seconds: float | None = None) -> ManagedCommandResult:
        del timeout_seconds
        return self.completed or ManagedCommandResult(
            args=self.args,
            pid=self.pid,
            exit_code=0,
            output="",
            state=ManagedCommandState.Exited,
        )

    def poll(self) -> ManagedCommandResult | None:
        self.poll_calls += 1
        if self.poll_calls <= self.complete_after_polls:
            return None
        return self.completed

    def terminate(self, *, timeout_seconds: float = 5) -> ManagedCommandResult:
        del timeout_seconds
        self.terminated = True
        return ManagedCommandResult(
            args=self.args,
            pid=self.pid,
            exit_code=-15,
            output="",
            state=ManagedCommandState.Terminated,
        )


@dataclass(slots=True)
class _AbortRunner(_Runner):
    deleted: bool = False

    def run(
        self,
        args: list[str],
        *,
        timeout_seconds: float,
        termination_timeout_seconds: float,
        max_output_chars: int,
    ) -> ProcessResult:
        if "delete" in args:
            self.commands.append(list(args))
            self.timeouts.append(timeout_seconds)
            self.deleted = True
            return ProcessResult(args=args, exit_code=0, stdout="", stderr="")
        if "state" in args and self.deleted:
            self.commands.append(list(args))
            self.timeouts.append(timeout_seconds)
            return ProcessResult(
                args=args,
                exit_code=1,
                stdout="",
                stderr="container does not exist",
            )
        return _Runner.run(
            self,
            args,
            timeout_seconds=timeout_seconds,
            termination_timeout_seconds=termination_timeout_seconds,
            max_output_chars=max_output_chars,
        )


def test_oci_runtime_aborts_inflight_run_when_started_callback_rejects(
    tmp_path: Path,
) -> None:
    runner = _AbortRunner()
    starter = _Starter()
    builder = OciRuntimeSpecBuilder(
        gateway_settings=GatewayServiceSettings(
            http=GatewayEndpointSettings(
                host=AgentBridgeNetworkConfig().gateway,
                port=AGENT_TUNNEL_CONTROL_PORT,
            )
        ),
        bundle_root=tmp_path / "bundles",
        image_mount_root=tmp_path / "images",
        runtime_configs={
            OciRuntimeName.Runsc: RuntimeBinaryConfig(
                runtime=OciRuntimeName.Runsc,
                runsc_path="/usr/bin/runsc",
            )
        },
        resolv_conf_source=_resolv_conf(tmp_path),
        fallback_resolv_conf_source=_resolv_conf(tmp_path),
    )
    spec = builder.build_spec(
        _context(tmp_path).model_copy(update={"runtime": OciRuntimeName.Runc}),
        image_config=ImageRuntimeConfig(),
        bind_ports=[],
        port_bindings=[],
        mount_result=ContainerMountSetupResult(),
        network_result=None,
        rootfs_result=_prepared_rootfs(tmp_path),
    )
    controller = OciRuntimeCommandController(
        run_command=runner.run,
        start_command=starter.start,
    )
    controller.prepare(spec)

    def reject_started(_pid: int) -> None:
        raise RuntimeError("container ctr-1 is stopping")

    with pytest.raises(RuntimeError, match="container ctr-1 is stopping"):
        controller.run(spec, on_started=reject_started)

    assert starter.managed[0].terminated
    assert any(command[-3:] == ["delete", "--force", "ctr-1"] for command in runner.commands)
    assert runner.commands[-1][-2:] == ["state", "ctr-1"]
    assert runner.deleted


def test_oci_runtime_bounds_hung_runsc_delete_during_start_abort(tmp_path: Path) -> None:
    term_marker = tmp_path / "delete-term"
    pid_path = tmp_path / "delete-pid"
    runsc = tmp_path / "runsc"
    runsc.write_text(
        "\n".join(
            (
                f"#!{sys.executable}",
                "import json",
                "import os",
                "import signal",
                "import sys",
                "import time",
                f"term_marker = {str(term_marker)!r}",
                f"pid_path = {str(pid_path)!r}",
                "if 'state' in sys.argv:",
                "    print(json.dumps({'id': 'ctr-1', 'pid': 4321, 'status': 'running'}))",
                "    raise SystemExit(0)",
                "if 'run' in sys.argv:",
                "    while True:",
                "        time.sleep(1)",
                "if 'delete' not in sys.argv:",
                "    raise SystemExit(0)",
                "def on_term(_signal, _frame):",
                "    open(term_marker, 'w', encoding='utf-8').write('term')",
                "signal.signal(signal.SIGTERM, on_term)",
                "open(pid_path, 'w', encoding='utf-8').write(str(os.getpid()))",
                "sys.stderr.write('delete-hung-' + ('x' * 100000))",
                "sys.stderr.flush()",
                "while True:",
                "    time.sleep(1)",
            )
        ),
        encoding="utf-8",
    )
    runsc.chmod(0o755)
    runtime_config = RuntimeBinaryConfig(
        runtime=OciRuntimeName.Runsc,
        runsc_path=str(runsc),
    )
    builder = OciRuntimeSpecBuilder(
        gateway_settings=GatewayServiceSettings(
            http=GatewayEndpointSettings(
                host=AgentBridgeNetworkConfig().gateway,
                port=AGENT_TUNNEL_CONTROL_PORT,
            )
        ),
        bundle_root=tmp_path / "bundles",
        image_mount_root=tmp_path / "images",
        runtime_configs={OciRuntimeName.Runsc: runtime_config},
        resolv_conf_source=_resolv_conf(tmp_path),
        fallback_resolv_conf_source=_resolv_conf(tmp_path),
    )
    spec = builder.build_spec(
        _context(tmp_path),
        image_config=ImageRuntimeConfig(),
        bind_ports=[],
        port_bindings=[],
        mount_result=ContainerMountSetupResult(),
        rootfs_result=_prepared_rootfs(tmp_path),
    )
    controller = OciRuntimeCommandController(
        runtime_config=runtime_config,
        command_timeouts=OciRuntimeCommandTimeouts(
            state_seconds=0.5,
            kill_seconds=0.5,
            delete_seconds=0.2,
            exec_seconds=1,
            checkpoint_seconds=1,
            termination_seconds=0.1,
            max_output_chars=256,
        ),
    )
    controller.prepare(spec)

    def reject_started(_pid: int) -> None:
        raise RuntimeError("container start was cancelled")

    started_at = time.monotonic()
    with pytest.raises(OciRuntimeCommandTimeout) as raised:
        controller.run(spec, on_started=reject_started)
    elapsed = time.monotonic() - started_at

    error = raised.value
    assert elapsed < 1.5
    assert error.operation.value == "delete"
    assert error.container_id == "ctr-1"
    assert error.process_reaped
    assert error.timeout_seconds == 0.2
    assert term_marker.read_text(encoding="utf-8") == "term"
    assert "earlier process output truncated" in error.detail
    assert len(error.detail) < 400
    assert controller.status("ctr-1") == "running"
    pid = int(pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def _resolv_conf(tmp_path: Path) -> Path:
    """A resolver the container can actually use.

    The host's own file points at a systemd-resolved stub on many machines,
    which the worker rightly refuses, so the test supplies its own rather than
    depending on how the developer's DNS happens to be configured.
    """
    source = tmp_path / "worker-resolv.conf"
    source.write_text("nameserver 1.1.1.1\n", encoding="utf-8")
    return source


def test_container_tmpfs_mounts_are_bounded_by_the_memory_request() -> None:
    """An unsized tmpfs lets a container reach its whole memory ceiling through it."""
    config = build_base_oci_config(tmpfs_size_mib=512)

    mounts = config["mounts"]
    assert isinstance(mounts, list)
    sized: dict[str, list[str]] = {}
    for mount in mounts:
        assert isinstance(mount, dict)
        destination = mount.get("destination")
        options = mount.get("options")
        if destination in {"/volumes", "/dev/shm"} and isinstance(options, list):
            sized[str(destination)] = [str(opt) for opt in options if str(opt).startswith("size=")]

    assert sized["/volumes"] == ["size=512m"]
    assert sized["/dev/shm"] == ["size=512m"]


def test_a_gpu_container_receives_device_nodes_and_permission_to_open_them() -> None:
    """Annotations describe an assignment; devices are what make it real.

    `/dev` is a fresh tmpfs with no devices and no OCI hook is injected, so the
    device nodes are the only thing that gives the container a GPU to open. They
    are also how gVisor decides a GPU was wanted at all: it looks for
    /dev/nvidiactl among them, or for an nvidia hook carrying
    NVIDIA_VISIBLE_DEVICES. The assertion runs on the prepared spec because
    preparation once cleared these devices after they were added, leaving
    --nvproxy enabled over a container gVisor had concluded wanted no GPU.
    """
    builder = OciRuntimeSpecBuilder()
    spec: dict[str, JsonValue] = build_base_oci_config()
    assignment = ContainerGpuAssignmentResult(
        container_id="gpu-container",
        requested_count=1,
        assigned_devices=[0],
        oci_devices=[
            OciDevice(path="/dev/nvidiactl", major=195, minor=255, file_mode=0o666),
            OciDevice(path="/dev/nvidia0", major=195, minor=0, file_mode=0o666),
        ],
        cdi_devices=["nvidia.com/gpu=0"],
    )

    builder._apply_gpu(spec, assignment)  # pyright: ignore[reportPrivateUsage]
    prepared = prepare_oci_spec_for_runtime(spec, OciRuntimeName.Runsc)
    assert prepared.nvproxy_enabled

    linux = prepared.spec["linux"]
    assert isinstance(linux, dict)
    devices = linux["devices"]
    assert isinstance(devices, list)
    assert [device["path"] for device in devices if isinstance(device, dict)] == [
        "/dev/nvidiactl",
        "/dev/nvidia0",
    ]
    resources = linux["resources"]
    assert isinstance(resources, dict)
    allowed = resources["devices"]
    assert isinstance(allowed, list)
    # A node without its cgroup rule is visible and unopenable.
    assert [(rule["major"], rule["minor"]) for rule in allowed if isinstance(rule, dict)] == [
        (195, 255),
        (195, 0),
    ]
    assert all(rule["allow"] is True for rule in allowed if isinstance(rule, dict))
