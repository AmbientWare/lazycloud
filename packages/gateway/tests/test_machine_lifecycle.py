from __future__ import annotations

from dataclasses import dataclass, field

from gateway.machine_lifecycle import MachineLifecycleService


@dataclass(slots=True)
class _Gateway:
    deleted: list[tuple[str, str]] = field(default_factory=list)

    def delete_machine(
        self,
        machine_id: str,
        *,
        workspace_id: str,
        pool_name: str = "",
    ) -> None:
        del pool_name
        self.deleted.append((workspace_id, machine_id))


@dataclass(slots=True)
class _ProviderCompute:
    released: list[tuple[str, str]] = field(default_factory=list)

    def release_bound_internal_pool_machine(
        self,
        machine_id: str,
        *,
        workspace: str,
    ) -> bool:
        self.released.append((workspace, machine_id))
        return True


def test_machine_deletion_releases_provider_capacity_before_gateway_cleanup() -> None:
    gateway = _Gateway()
    provider_compute = _ProviderCompute()
    lifecycle = MachineLifecycleService(
        gateway=gateway,
        provider_compute=provider_compute,
    )

    lifecycle.delete_machine(
        "11111111-1111-4111-8111-111111111111",
        workspace_id="22222222-2222-4222-8222-222222222222",
    )

    expected = (
        "22222222-2222-4222-8222-222222222222",
        "11111111-1111-4111-8111-111111111111",
    )
    assert provider_compute.released == [expected]
    assert gateway.deleted == [expected]
