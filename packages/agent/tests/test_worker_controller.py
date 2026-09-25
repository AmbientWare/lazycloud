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
from agent.worker_controller import CommandResult, DockerAgentWorkerController
from shared.contracts import ContractModel
from shared.placement import Placement
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
            return CommandResult(args=args, returncode=0, stdout="true\n\nhost")
        return CommandResult(args=args, returncode=0)


def test_worker_configuration_excludes_credentials_and_is_owner_readable(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    runner = _Runner()
    controller = DockerAgentWorkerController(
        state_dir=tmp_path,
        worker_image_override="container-worker:test",
        worker_network=AgentWorkerNetwork(name="host"),
        runner=runner,
    )
    request.addfinalizer(controller.close)
    preparation = controller.prepare_worker_image()
    assert preparation is not None
    preparation.result(timeout=5)
    slot = AgentWorkerSlot(
        worker_id="worker-one",
        worker_token="worker-secret",
        placement=Placement.machine("private-pool"),
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
        ),
        active_slots=[item.slot for item in controller.observe_workers()],
        reported_images=controller.prepared_worker_images(),
    )

    config_path = tmp_path / "slots" / "worker-one" / "worker.yaml"
    contents = config_path.read_text(encoding="utf-8")
    document = _WorkerConfigurationDocument.model_validate(yaml.safe_load(contents))
    config = document.configuration
    assert applied
    assert "worker-secret" not in contents
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert config.execution.capacity.cpu_millicores == slot.cpu_millicores


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
        placement=Placement.machine("private-pool"),
        billing_owner=UsageBillingOwner.SelfHosted,
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
    )

    bootstrap = AgentBootstrap(gateway_public_http_url="https://gateway.example.test")
    reported = controller.prepared_worker_images()
    controller.apply(
        plan_worker_slot_reconciliation([slot], []),
        bootstrap,
        active_slots=[],
        reported_images=reported,
    )
    assert [
        active.worker_id for active in [item.slot for item in controller.observe_workers()]
    ] == [slot.worker_id]

    controller.apply(
        plan_worker_slot_reconciliation([], [item.slot for item in controller.observe_workers()]),
        bootstrap,
        active_slots=[item.slot for item in controller.observe_workers()],
        reported_images=reported,
    )

    assert [item.slot for item in controller.observe_workers()] == []
