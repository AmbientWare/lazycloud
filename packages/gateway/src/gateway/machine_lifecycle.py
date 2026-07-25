from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from foundation.ids import try_uuid
from shared.errors import NotFoundError


class MachineDeletionGateway(Protocol):
    def delete_machine(
        self,
        machine_id: str,
        *,
        workspace_id: str,
        pool_name: str = "",
    ) -> None: ...


class ProviderMachineRelease(Protocol):
    def release_bound_internal_pool_machine(
        self,
        machine_id: str,
        *,
        workspace: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class MachineLifecycleService:
    gateway: MachineDeletionGateway
    provider_compute: ProviderMachineRelease | None = None

    def delete_machine(
        self,
        machine_id: str,
        *,
        workspace_id: str,
        pool_name: str = "",
    ) -> None:
        if try_uuid(machine_id) is None:
            # Machine ids are UUIDs; malformed ids are indistinguishable from
            # missing machines instead of leaking a database type error.
            msg = f"machine not found: {machine_id}"
            raise NotFoundError(msg)
        if self.provider_compute is not None:
            self.provider_compute.release_bound_internal_pool_machine(
                machine_id,
                workspace=workspace_id,
            )
        self.gateway.delete_machine(
            machine_id,
            workspace_id=workspace_id,
            pool_name=pool_name,
        )


__all__ = [
    "MachineDeletionGateway",
    "MachineLifecycleService",
    "ProviderMachineRelease",
]
