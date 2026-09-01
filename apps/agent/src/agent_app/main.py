from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from agent.operations import (
    AGENT_SOURCE_CACHE_RELATIVE_PATH,
    AgentCapacityOptions,
    AgentHostStatus,
    AgentResourceDetection,
    AgentState,
    AgentWorkerNetwork,
    AgentWorkerSlot,
    WorkerExecutor,
)
from agent.service_manager import (
    DEFAULT_AGENT_STATE_DIR,
    AgentRemoteLeaveResult,
    AgentServiceInstallResult,
    AgentServiceOperationResult,
    AgentServiceRuntimeStatus,
    AgentServiceSpec,
    RenderedServiceInstallPlan,
    ServiceCommand,
    ServiceCommandResult,
    ServiceLifecycleAction,
    ServicePlatform,
    build_agent_service_install_plan,
    build_agent_service_lifecycle_plan,
    resolve_service_platform,
)
from gateway.http import LeaveAgentRequest
from provider_clients import ProviderNodeIdentityEvidenceProvider
from pydantic import TypeAdapter, ValidationError
from shared.app_identity import AGENT_NAME
from shared.compute_policy import MachinePool
from shared.http.errors import HttpApiError
from shared.provider_config import ProviderKind
from worker.execution import (
    DEFAULT_CONTAINER_BRIDGE_NAME,
    DEFAULT_CONTAINER_IPV6_SUBNET,
    DEFAULT_CONTAINER_SUBNET,
)
from worker.source_cache_cleanup import (
    WorkerSourceCacheDestructionReceipt,
    destroy_source_cache_storage,
    source_cache_destruction_receipt,
)

from agent_app.daemon import (
    AGENT_AUTHORITY_REVOKED_DETAILS,
    AgentDaemonOptions,
    AgentDaemonRunResult,
    AgentGatewayClient,
    AgentLeaveClient,
    AgentPrivateNetworkRuntime,
    AgentProcessLock,
    DockerAgentWorkerController,
    HttpAgentGatewayClient,
    ProviderInstanceIdentityMode,
    build_agent_daemon_service,
)
from agent_app.route_proxy import AgentRouteProxyConfig


class AgentCommandArgs(argparse.Namespace):
    command: str
    gateway: str
    join_token: str
    join_token_file: str
    provider_enrollment_request: str
    provider: str | None
    provider_instance_identity: str | None
    state_dir: str
    hostname: str
    machine_fingerprint: str
    os_name: str
    arch: str
    executor: str
    worker_image: str
    worker_route_target: str
    worker_runtime_http_url: str
    worker_network: str
    worker_host_alias: list[str]
    max_cpu: str
    max_memory: str
    max_gpus: int
    gpu_ids: str
    docker_binary: str
    stream_interval_seconds: float
    http_timeout_seconds: float
    once: bool
    target: str
    service_name: str
    dry_run: bool
    keep_binary: bool


SOURCE_CACHE_DESTRUCTION_RECEIPT_FILE = "source-cache-destruction.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=AGENT_NAME)
    subcommands = parser.add_subparsers(dest="command", required=True)

    join = subcommands.add_parser("join")
    _add_daemon_options(join)

    install = subcommands.add_parser("install-service")
    _add_daemon_options(install, include_run_flags=False)
    install.add_argument(
        "--target",
        choices=["auto", ServicePlatform.Systemd.value, ServicePlatform.Launchd.value],
        default="auto",
    )
    install.add_argument("--service-name", default=AGENT_NAME)
    install.add_argument("--dry-run", action="store_true")

    status = subcommands.add_parser("status")
    _add_service_options(status, include_state_dir=True)
    for action in ("start", "restart", "leave", "uninstall"):
        lifecycle = subcommands.add_parser(action)
        _add_service_options(lifecycle, include_state_dir=action in {"leave", "uninstall"})
        if action == "uninstall":
            lifecycle.add_argument("--keep-binary", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv, namespace=AgentCommandArgs())
    try:
        if args.command == "join":
            result = run_agent_daemon(_daemon_options(args))
        elif args.command == "install-service":
            result = _install_service(args)
        elif args.command == "status":
            result = _status_payload(
                Path(args.state_dir),
                platform=_resolve_service_platform(args.target),
                service_name=args.service_name,
            )
        else:
            result = _manage_service(args)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")
    print(result.model_dump_json())
    if isinstance(result, AgentDaemonRunResult) and result.authority_revoked:
        # A revoked agent has finished for good. Exiting non-zero is what tells
        # the service manager this was not a clean stop to be restarted.
        parser.exit(1)


def _add_service_options(
    parser: argparse.ArgumentParser,
    *,
    include_state_dir: bool = False,
) -> None:
    parser.add_argument(
        "--target",
        choices=["auto", ServicePlatform.Systemd.value, ServicePlatform.Launchd.value],
        default="auto",
    )
    parser.add_argument("--service-name", default=AGENT_NAME)
    if include_state_dir:
        parser.add_argument("--state-dir", default=DEFAULT_AGENT_STATE_DIR)


def run_agent_daemon(
    options: AgentDaemonOptions,
    *,
    client: AgentGatewayClient | None = None,
    worker_controller: DockerAgentWorkerController | None = None,
    resource_detector: Callable[[], AgentResourceDetection] | None = None,
    private_network_runtime: AgentPrivateNetworkRuntime | None = None,
    provider_identity: ProviderNodeIdentityEvidenceProvider | None = None,
) -> AgentDaemonRunResult:
    _configure_daemon_logging()
    service = build_agent_daemon_service(
        options,
        client=client,
        worker_controller=worker_controller,
        resource_detector=resource_detector,
        private_network_runtime=private_network_runtime,
        provider_identity=provider_identity,
    )
    with AgentProcessLock.acquire(Path(options.state_dir)):
        return service.run()


def _configure_daemon_logging() -> None:
    """Give the daemon's own account somewhere to go.

    Without a handler Python emits nothing below `WARNING`, so a node under
    investigation offered one journal line saying the unit had started and
    nothing about what it then did. Records go to stderr because the commands
    in this module write machine-readable results to stdout.
    """
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _add_daemon_options(
    parser: argparse.ArgumentParser,
    *,
    include_run_flags: bool = True,
) -> None:
    parser.add_argument("--gateway", default="http://127.0.0.1:9000")
    parser.add_argument("--join-token", default="")
    parser.add_argument("--join-token-file", default="")
    parser.add_argument("--provider-enrollment-request", default="")
    parser.add_argument(
        "--provider",
        choices=[ProviderKind.Aws.value],
        default=None,
    )
    parser.add_argument(
        "--provider-instance-identity",
        choices=[item.value for item in ProviderInstanceIdentityMode],
        default=None,
    )
    parser.add_argument("--state-dir", default=DEFAULT_AGENT_STATE_DIR)
    parser.add_argument("--hostname", "--name", dest="hostname", default="")
    parser.add_argument("--machine-fingerprint", default="")
    parser.add_argument("--os", dest="os_name", default=platform.system().lower())
    parser.add_argument("--arch", default=platform.machine())
    parser.add_argument(
        "--executor",
        choices=[item.value for item in WorkerExecutor],
        default="container",
    )
    parser.add_argument("--worker-image", default="")
    parser.add_argument("--worker-route-target", default="127.0.0.1")
    parser.add_argument("--worker-runtime-http-url", default="")
    parser.add_argument("--worker-network", type=_agent_worker_network_name, default="host")
    # A second agent on the same host must build its containers on its own bridge:
    # each allocates addresses inside its own control-plane scope, so one shared
    # bridge is two allocators issuing one address with nothing between them.
    parser.add_argument("--worker-bridge-name", default=DEFAULT_CONTAINER_BRIDGE_NAME)
    parser.add_argument("--worker-bridge-subnet", default=DEFAULT_CONTAINER_SUBNET)
    parser.add_argument("--worker-bridge-ipv6-subnet", default=DEFAULT_CONTAINER_IPV6_SUBNET)
    parser.add_argument("--worker-host-alias", action="append", default=[])
    parser.add_argument("--max-cpu", default="")
    parser.add_argument("--max-memory", default="")
    parser.add_argument("--max-gpus", type=int, default=0)
    parser.add_argument("--gpu-ids", default="")
    parser.add_argument("--docker-binary", default="docker")
    parser.add_argument("--stream-interval-seconds", type=float, default=5.0)
    parser.add_argument("--http-timeout-seconds", type=float, default=30.0)
    if include_run_flags:
        parser.add_argument("--once", action="store_true")


def _agent_worker_network_name(value: str) -> str:
    try:
        return AgentWorkerNetwork(name=value).name
    except ValidationError as exc:
        message = "worker network must be a valid Docker network name"
        raise argparse.ArgumentTypeError(message) from exc


def _daemon_options(args: AgentCommandArgs) -> AgentDaemonOptions:
    return AgentDaemonOptions(
        gateway_url=args.gateway,
        join_token=args.join_token,
        join_token_file=args.join_token_file,
        provider_enrollment_request=args.provider_enrollment_request,
        provider=ProviderKind(args.provider) if args.provider else None,
        provider_instance_identity=(
            ProviderInstanceIdentityMode(args.provider_instance_identity)
            if args.provider_instance_identity
            else None
        ),
        state_dir=args.state_dir,
        machine_fingerprint=args.machine_fingerprint,
        hostname=args.hostname,
        os_name=args.os_name,
        arch=args.arch,
        executor=WorkerExecutor(args.executor),
        worker_image=args.worker_image,
        worker_route_target=args.worker_route_target,
        worker_runtime_http_url=args.worker_runtime_http_url,
        worker_network=AgentWorkerNetwork(
            name=args.worker_network,
            bridge_name=args.worker_bridge_name,
            bridge_subnet=args.worker_bridge_subnet,
            bridge_ipv6_subnet=args.worker_bridge_ipv6_subnet,
        ),
        worker_host_aliases=args.worker_host_alias,
        docker_binary=args.docker_binary,
        stream_interval_seconds=args.stream_interval_seconds,
        http_timeout_seconds=args.http_timeout_seconds,
        once=args.once,
        route_proxy=AgentRouteProxyConfig(),
        capacity=AgentCapacityOptions(
            max_cpu=args.max_cpu,
            max_memory=args.max_memory,
            max_gpus=args.max_gpus,
            gpu_ids=args.gpu_ids,
        ),
    )


def _install_service(args: AgentCommandArgs) -> AgentServiceInstallResult:
    selected_platform = _resolve_service_platform(args.target)
    if not args.dry_run:
        _require_service_manager(selected_platform, mutation=True)
        _require_container_runtime(args)
    token_path = _prepare_join_token_file(args) if not args.dry_run else _join_token_path(args)
    command = _install_service_command(
        args,
        token_path=token_path,
        binary_path=_agent_binary_path(),
    )
    plan = build_agent_service_install_plan(
        AgentServiceSpec(
            name=args.service_name,
            binary_path=command[0],
            args=command[1:],
            state_dir=args.state_dir,
        ),
        platform=selected_platform,
        root=os.getuid() == 0,
        uid=os.getuid(),
    )
    results: list[ServiceCommandResult] = []
    if not args.dry_run:
        _write_service_plan(plan)
        results = _run_service_commands(
            [
                ServiceCommand(
                    argv=service_command,
                    allow_failure=plan.platform is ServicePlatform.Launchd and index == 0,
                )
                for index, service_command in enumerate(plan.commands)
            ]
        )
    return AgentServiceInstallResult(
        platform=plan.platform,
        service_name=plan.spec.name,
        service_path=plan.target_path,
        command=command,
        service_commands=plan.commands,
        installed=not args.dry_run,
        dry_run=args.dry_run,
        commands=results,
    )


def _install_service_command(
    args: AgentCommandArgs,
    *,
    token_path: Path | None = None,
    binary_path: str = AGENT_NAME,
) -> list[str]:
    command = [
        binary_path,
        "join",
        "--gateway",
        args.gateway,
        "--state-dir",
        args.state_dir,
        "--executor",
        args.executor,
    ]
    if token_path is not None:
        command.extend(["--join-token-file", str(token_path)])
    elif args.join_token_file:
        command.extend(["--join-token-file", args.join_token_file])
    elif args.join_token:
        command.extend(["--join-token", args.join_token])
    if args.provider_enrollment_request:
        command.extend(["--provider-enrollment-request", args.provider_enrollment_request])
    if args.provider:
        command.extend(["--provider", args.provider])
    if args.provider_instance_identity:
        command.extend(["--provider-instance-identity", args.provider_instance_identity])
    if args.hostname:
        command.extend(["--hostname", args.hostname])
    if args.machine_fingerprint:
        command.extend(["--machine-fingerprint", args.machine_fingerprint])
    if args.worker_image:
        command.extend(["--worker-image", args.worker_image])
    if args.worker_route_target != "127.0.0.1":
        command.extend(["--worker-route-target", args.worker_route_target])
    if args.worker_runtime_http_url:
        command.extend(["--worker-runtime-http-url", args.worker_runtime_http_url])
    if args.worker_network != "host":
        command.extend(["--worker-network", args.worker_network])
    # Re-emitted rather than left to the default: the installed unit is what runs from
    # here on, and an agent that silently reverted to the shared bridge would collide
    # with whichever other agent owns it.
    if args.worker_bridge_name != DEFAULT_CONTAINER_BRIDGE_NAME:
        command.extend(["--worker-bridge-name", args.worker_bridge_name])
    if args.worker_bridge_subnet != DEFAULT_CONTAINER_SUBNET:
        command.extend(["--worker-bridge-subnet", args.worker_bridge_subnet])
    if args.worker_bridge_ipv6_subnet != DEFAULT_CONTAINER_IPV6_SUBNET:
        command.extend(["--worker-bridge-ipv6-subnet", args.worker_bridge_ipv6_subnet])
    for alias in args.worker_host_alias:
        command.extend(["--worker-host-alias", alias])
    if args.max_cpu:
        command.extend(["--max-cpu", args.max_cpu])
    if args.max_memory:
        command.extend(["--max-memory", args.max_memory])
    if args.max_gpus > 0:
        command.extend(["--max-gpus", str(args.max_gpus)])
    if args.gpu_ids:
        command.extend(["--gpu-ids", args.gpu_ids])
    if args.docker_binary != "docker":
        command.extend(["--docker-binary", args.docker_binary])
    return command


def _join_token_path(args: AgentCommandArgs) -> Path | None:
    if args.provider_enrollment_request:
        return None
    if args.join_token_file:
        return Path(args.join_token_file)
    if args.join_token:
        return Path(args.state_dir) / "join-token"
    return None


def _prepare_join_token_file(args: AgentCommandArgs) -> Path | None:
    if args.provider_enrollment_request:
        return None
    if args.join_token_file:
        token_path = Path(args.join_token_file)
        if not token_path.is_file():
            msg = f"join token file does not exist: {token_path}"
            raise ValueError(msg)
        return token_path
    if not args.join_token:
        msg = "--join-token or --join-token-file is required"
        raise ValueError(msg)
    token_path = Path(args.state_dir) / "join-token"
    _write_file_atomic(token_path, args.join_token.strip() + "\n", permissions=0o600)
    return token_path


def _agent_binary_path() -> str:
    raw = Path(sys.argv[0])
    if raw.name == AGENT_NAME and (raw.is_absolute() or raw.parent != Path(".")):
        return str(raw)
    return shutil.which(AGENT_NAME) or AGENT_NAME


def _write_service_plan(plan: RenderedServiceInstallPlan) -> None:
    if not plan.target_path or not plan.content:
        return
    target = Path(plan.target_path).expanduser()
    _write_file_atomic(target, plan.content, permissions=0o644)


def _write_file_atomic(path: Path, contents: str, *, permissions: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if permissions == 0o600:
        path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, permissions)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, permissions)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _run_service_commands(commands: list[ServiceCommand]) -> list[ServiceCommandResult]:
    results: list[ServiceCommandResult] = []
    for command in commands:
        completed = subprocess.run(command.argv, capture_output=True, text=True, check=False)
        results.append(ServiceCommandResult(argv=command.argv, returncode=completed.returncode))
        if completed.returncode and not command.allow_failure:
            detail = completed.stderr.strip() or completed.stdout.strip()
            suffix = f": {detail}" if detail else ""
            msg = f"{command.argv[0]} exited with {completed.returncode}{suffix}"
            raise RuntimeError(msg)
    return results


def _resolve_service_platform(target: str) -> ServicePlatform:
    return resolve_service_platform(target, os_name=sys.platform)


def _require_service_manager(platform: ServicePlatform, *, mutation: bool) -> None:
    command = "systemctl" if platform is ServicePlatform.Systemd else "launchctl"
    if shutil.which(command) is None:
        msg = f"{command} is required for {platform.value} service management"
        raise RuntimeError(msg)
    if platform is ServicePlatform.Systemd:
        if not Path("/run/systemd/system").is_dir():
            msg = "systemd is not running; run the agent in the foreground with 'join'"
            raise RuntimeError(msg)
        if mutation and os.getuid() != 0:
            msg = "systemd service changes require root"
            raise RuntimeError(msg)


def _require_container_runtime(args: AgentCommandArgs) -> None:
    if WorkerExecutor(args.executor) is WorkerExecutor.External:
        return
    docker_binary = args.docker_binary
    if shutil.which(docker_binary) is None:
        msg = (
            f"Docker binary '{docker_binary}' was not found; use the canonical installer "
            "or install Docker before installing the service"
        )
        raise RuntimeError(msg)
    completed = subprocess.run(
        [docker_binary, "info"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        msg = (
            "Docker is installed but unavailable; start Docker and verify this user can run "
            f"'{docker_binary} info'"
        )
        raise RuntimeError(msg)


def _service_status(
    *,
    platform: ServicePlatform,
    service_name: str,
) -> AgentServiceRuntimeStatus:
    plan = build_agent_service_lifecycle_plan(
        ServiceLifecycleAction.Status,
        platform=platform,
        service_name=service_name,
        root=os.getuid() == 0,
        uid=os.getuid(),
    )
    target = Path(plan.target_path).expanduser()
    installed = target.is_file()
    command_name = "systemctl" if platform is ServicePlatform.Systemd else "launchctl"
    if shutil.which(command_name) is None:
        return AgentServiceRuntimeStatus(
            platform=platform,
            service_name=plan.service_name,
            target_path=str(target),
            installed=installed,
        )
    completed = [
        subprocess.run(command.argv, capture_output=True, text=True, check=False)
        for command in plan.before_removal
    ]
    if platform is ServicePlatform.Systemd:
        active = bool(completed) and completed[0].stdout.strip() == "active"
        enabled = len(completed) > 1 and completed[1].stdout.strip() in {
            "enabled",
            "enabled-runtime",
            "linked",
            "linked-runtime",
            "static",
        }
    else:
        active = bool(completed) and completed[0].returncode == 0
        enabled = installed
    return AgentServiceRuntimeStatus(
        platform=platform,
        service_name=plan.service_name,
        target_path=str(target),
        installed=installed,
        active=active,
        enabled=enabled,
    )


def _manage_service(args: AgentCommandArgs) -> AgentServiceOperationResult:
    action = ServiceLifecycleAction(args.command)
    selected_platform = _resolve_service_platform(args.target)
    _require_service_manager(selected_platform, mutation=True)
    plan = build_agent_service_lifecycle_plan(
        action,
        platform=selected_platform,
        service_name=args.service_name,
        root=os.getuid() == 0,
        uid=os.getuid(),
    )
    service_path = Path(plan.target_path).expanduser()
    state_dir: Path | None = None
    saved_state: AgentState | None = None
    if plan.remove_state:
        state_dir = _validated_state_directory(Path(args.state_dir))
        saved_state = _load_agent_state_for_removal(state_dir, service_path=service_path)
    commands = _run_service_commands(plan.before_removal)
    remote_leave: AgentRemoteLeaveResult | None = None
    if plan.remove_state and state_dir is not None:
        if _service_status(platform=selected_platform, service_name=plan.service_name).active:
            msg = f"{plan.service_name} is still running; local and remote state were preserved"
            raise RuntimeError(msg)
        if saved_state is not None:
            DockerAgentWorkerController(state_dir=state_dir).stop_all()
            cache_destruction = _prepare_source_cache_destruction(state_dir, saved_state)
            remote_leave = _leave_remote_agent(
                saved_state,
                cache_destruction=cache_destruction,
            )
    service_removed = False
    state_removed = False
    binary_removed = False
    if plan.remove_service:
        service_removed = service_path.exists()
        service_path.unlink(missing_ok=True)
    if state_dir is not None:
        state_removed = state_dir.exists()
        if state_removed:
            shutil.rmtree(state_dir)
    commands.extend(_run_service_commands(plan.after_removal))
    if action is ServiceLifecycleAction.Uninstall and not args.keep_binary:
        binary_removed = _remove_canonical_agent_binary()
    return AgentServiceOperationResult(
        action=action,
        platform=selected_platform,
        service_name=plan.service_name,
        service_path=str(service_path),
        commands=commands,
        remote_leave=remote_leave,
        service_removed=service_removed,
        state_removed=state_removed,
        binary_removed=binary_removed,
    )


def _load_agent_state_for_removal(state_dir: Path, *, service_path: Path) -> AgentState | None:
    state_path = state_dir / "agent-state.json"
    if not state_path.exists():
        if state_dir.exists() or service_path.exists():
            msg = (
                f"saved agent identity is missing: {state_path}; "
                "regenerate the machine join command before removing this service"
            )
            raise RuntimeError(msg)
        return None
    try:
        state = AgentState.model_validate_json(state_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        msg = f"saved agent identity is unreadable or invalid: {state_path}"
        raise RuntimeError(msg) from exc
    if (
        not state.agent_token.strip()
        or not state.machine_id.strip()
        or not state.gateway_url.strip()
    ):
        msg = f"saved agent identity is incomplete: {state_path}"
        raise RuntimeError(msg)
    return state


def _leave_remote_agent(
    state: AgentState,
    *,
    client: AgentLeaveClient | None = None,
    cache_destruction: WorkerSourceCacheDestructionReceipt | None = None,
) -> AgentRemoteLeaveResult:
    gateway_client = client or HttpAgentGatewayClient.from_options(
        AgentDaemonOptions(gateway_url=state.gateway_url)
    )
    try:
        response = gateway_client.leave_agent(
            LeaveAgentRequest(
                agent_token=state.agent_token,
                machine_id=state.machine_id,
                cache_generation_id=(
                    cache_destruction.generation_id if cache_destruction is not None else ""
                ),
                cache_session_fence=(
                    cache_destruction.session_fence if cache_destruction is not None else None
                ),
            )
        )
    except HttpApiError as exc:
        if _remote_agent_already_absent(exc):
            return AgentRemoteLeaveResult(machine_id=state.machine_id, already_absent=True)
        raise
    if response.machine_id != state.machine_id:
        msg = "gateway leave response did not match the saved machine identity"
        raise RuntimeError(msg)
    return AgentRemoteLeaveResult(machine_id=response.machine_id)


def _prepare_source_cache_destruction(
    state_dir: Path,
    state: AgentState,
) -> WorkerSourceCacheDestructionReceipt | None:
    receipt_path = state_dir / SOURCE_CACHE_DESTRUCTION_RECEIPT_FILE
    storage_id = f"machine:{state.machine_id}"
    if receipt_path.exists():
        try:
            receipt = WorkerSourceCacheDestructionReceipt.model_validate_json(
                receipt_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            raise RuntimeError(
                f"source cache destruction receipt is invalid: {receipt_path}"
            ) from exc
        if receipt.storage_id != storage_id:
            raise RuntimeError("source cache destruction receipt does not match saved machine")
        destroy_source_cache_storage(
            state_dir / AGENT_SOURCE_CACHE_RELATIVE_PATH,
            receipt,
        )
        return receipt
    cache_root = state_dir / AGENT_SOURCE_CACHE_RELATIVE_PATH
    receipt = source_cache_destruction_receipt(cache_root, storage_id=storage_id)
    if receipt is None:
        return None
    temporary = receipt_path.with_name(f".{receipt_path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(receipt.model_dump_json())
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, receipt_path)
        directory_descriptor = os.open(state_dir, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    destroy_source_cache_storage(cache_root, receipt)
    return receipt


def _remote_agent_already_absent(exc: HttpApiError) -> bool:
    if exc.status_code != 400:
        return False
    detail = (exc.detail or str(exc)).strip().lower()
    return detail in AGENT_AUTHORITY_REVOKED_DETAILS


def _validated_state_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    protected = {Path("/"), Path("/var"), Path("/var/lib"), Path("/tmp"), Path.home()}
    if resolved in protected or len(resolved.parts) < 3:
        msg = f"refusing to remove unsafe agent state directory: {resolved}"
        raise ValueError(msg)
    return resolved


def _remove_canonical_agent_binary() -> bool:
    binary = Path(_agent_binary_path()).expanduser().resolve()
    allowed = {
        Path("/usr/local/bin") / AGENT_NAME,
        Path.home() / f".{AGENT_NAME.removesuffix('-agent')}" / "bin" / AGENT_NAME,
    }
    if binary not in allowed or not binary.is_file():
        return False
    binary.unlink()
    return True


def _status_payload(
    state_dir: Path,
    *,
    platform: ServicePlatform,
    service_name: str,
) -> AgentHostStatus:
    state_path = state_dir / "agent-state.json"
    active_slots_path = state_dir / "active-worker-slots.json"
    state: AgentState | None = None
    if state_path.exists():
        try:
            state = AgentState.model_validate_json(state_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            msg = f"agent state is unreadable or invalid: {state_path}"
            raise RuntimeError(msg) from exc
    active_worker_count = 0
    if active_slots_path.exists():
        try:
            active = TypeAdapter(list[AgentWorkerSlot]).validate_json(
                active_slots_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            msg = f"agent worker state is unreadable or invalid: {active_slots_path}"
            raise RuntimeError(msg) from exc
        active_worker_count = len(active)
    service = _service_status(platform=platform, service_name=service_name)
    return AgentHostStatus(
        joined=state is not None,
        state_path=str(state_path),
        active_worker_count=active_worker_count,
        workspace_id=state.workspace_id if state else "",
        pool=MachinePool(state.pool if state else ""),
        machine_id=state.machine_id if state else "",
        gateway_url=state.sanitized_gateway_url if state else "",
        service=service,
    )


if __name__ == "__main__":
    main()
