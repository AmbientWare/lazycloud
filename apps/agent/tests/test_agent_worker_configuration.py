from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

import pytest
import yaml
from agent.operations import (
    AgentBootstrap,
    AgentWorkerNetwork,
    AgentWorkerSlot,
    plan_worker_slot_reconciliation,
)
from agent_app.daemon import CommandResult, DockerAgentWorkerController
from shared.compute_policy import MachinePool
from shared.contracts import ContractModel
from shared.routing import BackendRouteTransport
from shared.usage import UsageBillingOwner
from worker.configuration import WorkerConfiguration


class _WorkerConfigurationDocument(ContractModel):
    configuration: WorkerConfiguration


@dataclass(slots=True)
class _Runner:
    calls: list[list[str]] = field(default_factory=list)

    def run(self, args: list[str], *, stop: Event | None = None) -> CommandResult:
        del stop
        self.calls.append(args)
        return CommandResult(args=args, returncode=0)


@dataclass(slots=True)
class _RunningWorkerRunner(_Runner):
    def run(self, args: list[str], *, stop: Event | None = None) -> CommandResult:
        del stop
        self.calls.append(args)
        if len(args) > 1 and args[1] == "inspect":
            return CommandResult(args=args, returncode=0, stdout="true")
        return CommandResult(args=args, returncode=0)


def test_agent_atomically_writes_worker_yaml_before_starting_container(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    runner = _Runner()
    controller = DockerAgentWorkerController(
        state_dir=tmp_path,
        worker_image_override="container-worker:test",
        worker_network=AgentWorkerNetwork(name="lazycloud_default"),
        runner=runner,
    )
    request.addfinalizer(controller.close)
    preparation = controller.prepare_worker_image()
    assert preparation is not None
    preparation.result(timeout=5)
    slot = AgentWorkerSlot(
        worker_id="worker-one",
        worker_token="worker-secret",
        pool=MachinePool("private-pool"),
        billing_owner=UsageBillingOwner.SelfHosted,
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        machine_id="machine-one",
        cpu_millicores=4000,
        memory_mb=8192,
        gpu="L4",
        gpu_count=1,
        gpu_assignment="0",
        network_prefix="private-pool:machine-one",
    )

    applied = controller.apply(
        plan_worker_slot_reconciliation([slot], []),
        AgentBootstrap(
            gateway_public_http_url="https://gateway.example.test",
            gateway_runtime_http_url="http://host.docker.internal:8000",
            transport=BackendRouteTransport.PrivateNetwork,
        ),
    )

    config_path = tmp_path / "slots" / "worker-one" / "worker.yaml"
    contents = config_path.read_text(encoding="utf-8")
    document = _WorkerConfigurationDocument.model_validate(yaml.safe_load(contents))
    config = document.configuration
    docker_run = next(args for args in runner.calls if len(args) > 1 and args[1] == "run")

    assert applied
    assert config.execution.capacity.cpu_millicores == 4000
    assert config.execution.capacity.memory_mib == 8192
    assert config.execution.capacity.gpu_type == "L4"
    assert config.execution.capacity.gpu_count == 1
    assert "WORKER_NETWORK_PREFIX=private-pool:machine-one" in docker_run
    assert "worker-secret" not in contents
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert f"{config_path}:/etc/lazycloud/worker/worker.yaml:ro" in docker_run
    # Both clients must reach the control plane through the runtime origin.
    assert "WORKER_REPOSITORY_URL=http://host.docker.internal:8000" in docker_run
    assert "GATEWAY_HTTP_URL=http://host.docker.internal:8000" in docker_run
    assert "WORKER_ROUTE_TARGET=127.0.0.1" in docker_run
    assert docker_run[docker_run.index("--network") + 1] == "lazycloud_default"
    assert docker_run[docker_run.index("--cgroupns") + 1] == "host"


def test_agent_gives_all_workers_one_bounded_graceful_shutdown_window(
    tmp_path: Path,
) -> None:
    runner = _RunningWorkerRunner()
    controller = DockerAgentWorkerController(state_dir=tmp_path, runner=runner)
    slots = [
        AgentWorkerSlot(
            worker_id=f"worker-{index}",
            pool=MachinePool("private-pool"),
            billing_owner=UsageBillingOwner.SelfHosted,
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            machine_id="machine-one",
        )
        for index in (1, 2)
    ]
    controller._save_active_slots(slots)

    controller.gracefully_stop_all(grace_seconds=89.1)

    assert [
        "docker",
        "stop",
        "--timeout",
        "90",
        "lazycloud-agent-worker-1",
        "lazycloud-agent-worker-2",
    ] in runner.calls
    assert [
        "docker",
        "rm",
        "-f",
        "lazycloud-agent-worker-1",
        "lazycloud-agent-worker-2",
    ] in runner.calls
    assert controller.active_slots() == []


@dataclass(slots=True)
class _ConcurrentRemovalRunner(_RunningWorkerRunner):
    def run(self, args: list[str], *, stop: Event | None = None) -> CommandResult:
        if len(args) > 1 and args[1] == "rm":
            self.calls.append(args)
            return CommandResult(
                args=args,
                returncode=1,
                stderr=(
                    "Error response from daemon: removal of container "
                    "lazycloud-agent-worker-one is already in progress"
                ),
            )
        return _RunningWorkerRunner.run(self, args, stop=stop)


def test_agent_stop_treats_concurrent_container_removal_as_settled(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    runner = _ConcurrentRemovalRunner()
    controller = DockerAgentWorkerController(
        state_dir=tmp_path, runner=runner, worker_image_override="container-worker:test"
    )
    request.addfinalizer(controller.close)
    preparation = controller.prepare_worker_image()
    assert preparation is not None
    preparation.result(timeout=5)
    slot = AgentWorkerSlot(
        worker_id="worker-one",
        worker_token="worker-secret",
        pool=MachinePool("private-pool"),
        billing_owner=UsageBillingOwner.SelfHosted,
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
    )

    bootstrap = AgentBootstrap(gateway_public_http_url="https://gateway.example.test")
    controller.apply(plan_worker_slot_reconciliation([slot], []), bootstrap)
    assert [active.worker_id for active in controller.active_slots()] == [slot.worker_id]

    controller.apply(plan_worker_slot_reconciliation([], controller.active_slots()), bootstrap)

    assert controller.active_slots() == []
