from __future__ import annotations

from dataclasses import dataclass

import pytest
from gateway.machine_lifecycle import MachineLifecycleService
from shared.compute_policy import MachinePool
from shared.errors import NotFoundError

MACHINE_ID = "11111111-1111-4111-8111-111111111111"
WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"


@dataclass(slots=True)
class _Gateway:
    machines: set[tuple[str, str]]

    def delete_machine(
        self,
        machine_id: str,
        *,
        workspace_id: str,
        pool: MachinePool = MachinePool(""),
    ) -> None:
        del pool
        self.machines.remove((workspace_id, machine_id))


@dataclass(slots=True)
class _ProviderCompute:
    machines: set[tuple[str, str]]
    unavailable: bool = False

    def release_bound_internal_pool_machine(
        self,
        machine_id: str,
        *,
        workspace: str,
    ) -> bool:
        if self.unavailable:
            raise ConnectionError("provider unavailable")
        self.machines.remove((workspace, machine_id))
        return True


def test_failed_machine_release_keeps_authority_until_cleanup_can_retry() -> None:
    gateway = _Gateway({(WORKSPACE_ID, MACHINE_ID)})
    provider = _ProviderCompute({(WORKSPACE_ID, MACHINE_ID)}, unavailable=True)
    lifecycle = MachineLifecycleService(
        gateway=gateway,
        provider_compute=provider,
    )

    with pytest.raises(ConnectionError, match="provider unavailable"):
        lifecycle.delete_machine(MACHINE_ID, workspace_id=WORKSPACE_ID)

    assert gateway.machines == provider.machines == {(WORKSPACE_ID, MACHINE_ID)}
    provider.unavailable = False
    lifecycle.delete_machine(MACHINE_ID, workspace_id=WORKSPACE_ID)

    assert not gateway.machines
    assert not provider.machines


def test_machine_deletion_rejects_a_malformed_machine_id_without_touching_capacity() -> None:
    gateway = _Gateway({(WORKSPACE_ID, MACHINE_ID)})
    provider = _ProviderCompute({(WORKSPACE_ID, MACHINE_ID)})
    lifecycle = MachineLifecycleService(
        gateway=gateway,
        provider_compute=provider,
    )

    with pytest.raises(NotFoundError):
        lifecycle.delete_machine("not-a-uuid", workspace_id=WORKSPACE_ID)

    assert gateway.machines == provider.machines == {(WORKSPACE_ID, MACHINE_ID)}
