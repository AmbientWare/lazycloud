from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from api.server.services import ApiServices
from compute.offers import ComputeOffer
from compute.providers import (
    DirectMachineLaunchRequest,
    ProviderMachineReference,
    ProviderMachineStatus,
    ProviderReconcileResult,
)


@dataclass(frozen=True, slots=True)
class RecordingProviderMachine:
    id: str
    name: str
    machine_id: str
    pool_name: str
    status: str = ProviderMachineStatus.Active


@dataclass
class RecordingDirectMachineProvider:
    name: str
    offers: list[ComputeOffer]
    machines: list[RecordingProviderMachine] = field(default_factory=list)

    def list_offers(self) -> list[ComputeOffer]:
        return [offer.model_copy(update={"provider": self.name}) for offer in self.offers]

    def launch_machine(self, request: DirectMachineLaunchRequest) -> ProviderMachineReference:
        machine = RecordingProviderMachine(
            id=request.machine_id,
            name=f"{self.name}-{request.pool_name}-{request.machine_id}",
            machine_id=request.machine_id,
            pool_name=request.pool_name,
        )
        self.machines.append(machine)
        return ProviderMachineReference(
            provider_instance_id=machine.id,
            machine_id=machine.machine_id,
            name=machine.name,
            status=machine.status,
            storage_volume_ids=(f"volume-{machine.id}",),
        )

    def list_machines(self, pool_name: str) -> list[RecordingProviderMachine]:
        return [
            machine
            for machine in self.machines
            if machine.pool_name == pool_name and machine.status != ProviderMachineStatus.Terminated
        ]

    def reconcile_machines(
        self,
        pool_name: str,
        expected_machine_ids: set[str],
        *,
        terminate_stale: bool = False,
    ) -> ProviderReconcileResult:
        known_ids = {
            machine.machine_id for machine in self.machines if machine.pool_name == pool_name
        }
        live_ids = {machine.machine_id for machine in self.list_machines(pool_name)}
        stale = sorted(live_ids - expected_machine_ids)
        terminated: list[str] = []
        if terminate_stale:
            for machine_id in stale:
                self.terminate_machine(machine_id)
                if self.machine_storage_destroyed(machine_id, (f"volume-{machine_id}",)):
                    terminated.append(machine_id)
        return ProviderReconcileResult(
            observed_machines=[
                ProviderMachineReference(
                    provider_instance_id=machine.id,
                    machine_id=machine.machine_id,
                    name=machine.name,
                    status=machine.status,
                    storage_volume_ids=(f"volume-{machine.id}",),
                )
                for machine in self.list_machines(pool_name)
                if machine.machine_id in expected_machine_ids
            ],
            missing_machine_ids=sorted((expected_machine_ids & known_ids) - live_ids),
            stale_machine_ids=stale,
            terminated_machine_ids=terminated,
        )

    def terminate_machine(self, provider_instance_id: str, /) -> None:
        for index, machine in enumerate(self.machines):
            if machine.id == provider_instance_id or machine.machine_id == provider_instance_id:
                self.machines[index] = RecordingProviderMachine(
                    id=machine.id,
                    name=machine.name,
                    machine_id=machine.machine_id,
                    pool_name=machine.pool_name,
                    status=ProviderMachineStatus.Terminated,
                )
                return

    def machine_storage_destroyed(
        self,
        provider_instance_id: str,
        storage_volume_ids: tuple[str, ...],
        /,
    ) -> bool:
        if not storage_volume_ids:
            return False
        return all(
            not (machine.id == provider_instance_id or machine.machine_id == provider_instance_id)
            or machine.status == ProviderMachineStatus.Terminated
            for machine in self.machines
        )


@dataclass(slots=True)
class RecordingProviderRegistry:
    providers_by_workspace: dict[str, dict[str, RecordingDirectMachineProvider]] = field(
        default_factory=dict
    )

    def snapshot(self, workspace: str) -> Mapping[str, RecordingDirectMachineProvider]:
        providers = self.providers_by_workspace.get(workspace, {})
        return MappingProxyType(dict(sorted(providers.items())))

    def snapshot_for_workspace_deletion(
        self,
        workspace_id: str,
    ) -> Mapping[str, RecordingDirectMachineProvider]:
        providers = self.providers_by_workspace.get(workspace_id, {})
        return MappingProxyType(dict(sorted(providers.items())))

    def set(self, workspace: str, provider: RecordingDirectMachineProvider) -> None:
        self.providers_by_workspace.setdefault(workspace, {})[provider.name] = provider


def configure_test_provider(
    services: ApiServices,
    name: str,
    offers: Iterable[ComputeOffer],
    *,
    workspace: str = "default",
) -> RecordingDirectMachineProvider:
    with services.context.database.session() as session:
        workspace_id = services.context.workspace(session, workspace).id
    registry = services.compute.provider_registry
    if not isinstance(registry, RecordingProviderRegistry):
        registry = RecordingProviderRegistry()
        services.compute.provider_registry = registry
        services.compute.provider_resolver = None
    provider = RecordingDirectMachineProvider(name=name, offers=list(offers))
    registry.set(workspace_id, provider)
    return provider


__all__ = [
    "RecordingDirectMachineProvider",
    "RecordingProviderMachine",
    "RecordingProviderRegistry",
    "configure_test_provider",
]
