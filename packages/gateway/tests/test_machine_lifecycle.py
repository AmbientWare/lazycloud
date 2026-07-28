from __future__ import annotations

from dataclasses import dataclass

import pytest
from gateway.machine_lifecycle import MachineLifecycleService
from shared.errors import NotFoundError

MACHINE_ID = "11111111-1111-4111-8111-111111111111"
WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"


@dataclass(slots=True)
class _Gateway:
    calls: list[tuple[str, str, str]]

    def delete_machine(
        self,
        machine_id: str,
        *,
        workspace_id: str,
        pool_name: str = "",
    ) -> None:
        del pool_name
        self.calls.append(("delete", workspace_id, machine_id))


@dataclass(slots=True)
class _ProviderCompute:
    calls: list[tuple[str, str, str]]

    def release_bound_internal_pool_machine(
        self,
        machine_id: str,
        *,
        workspace: str,
    ) -> bool:
        self.calls.append(("release", workspace, machine_id))
        return True


def test_machine_deletion_releases_provider_capacity_before_gateway_cleanup() -> None:
    # Deleting the gateway record first strands the paid provider machine:
    # nothing is left that names the capacity still to be released.
    calls: list[tuple[str, str, str]] = []
    lifecycle = MachineLifecycleService(
        gateway=_Gateway(calls),
        provider_compute=_ProviderCompute(calls),
    )

    lifecycle.delete_machine(MACHINE_ID, workspace_id=WORKSPACE_ID)

    assert calls == [
        ("release", WORKSPACE_ID, MACHINE_ID),
        ("delete", WORKSPACE_ID, MACHINE_ID),
    ]


def test_machine_deletion_rejects_a_malformed_machine_id_without_touching_capacity() -> None:
    calls: list[tuple[str, str, str]] = []
    lifecycle = MachineLifecycleService(
        gateway=_Gateway(calls),
        provider_compute=_ProviderCompute(calls),
    )

    with pytest.raises(NotFoundError):
        lifecycle.delete_machine("not-a-uuid", workspace_id=WORKSPACE_ID)

    assert calls == []
