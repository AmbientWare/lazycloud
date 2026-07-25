"""In-memory pooled-capacity provider for dev/test bootstrap iteration.

The fake provider implements the exact ``compute.providers.PooledCapacityProvider``
protocol so the real compute reconciliation, enrollment, and reclaim owners can
run the full provision -> enroll -> ready -> reclaim cycle against real
PostgreSQL/Redis without any AWS dependency. It never advances bootstrap phases
itself: like AWS, it reports launched instances as ``active`` regardless of
in-guest bootstrap progress, and bootstrap outcomes are driven by the node
agent (real or fake) through the production enrollment service.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from compute.offers import ComputeOffer
from compute.providers import (
    ProviderCapacityPhase,
    ProviderMachineStatus,
    ProviderPoolInstance,
    ProviderPoolRequest,
    ProviderPoolSnapshot,
)
from shared.compute_policy import ComputePoolProviderState
from shared.timestamps import utc_now


@dataclass(frozen=True, slots=True)
class FakeInstance:
    """One launched fake machine and its storage volume."""

    instance_id: str
    storage_volume_ids: tuple[str, ...]
    launched_at: datetime


@dataclass(slots=True)
class _FakePoolState:
    resource_id: str
    desired_machines: int = 0
    max_machines: int = 0
    deleted: bool = False
    instances: dict[str, FakeInstance] = field(default_factory=dict)


@dataclass(slots=True)
class FakePooledCapacityProvider:
    """Deterministic in-memory ``PooledCapacityProvider`` keyed by pool id.

    ``clock`` is injectable so dev loops can drive time-based reclaim decisions
    without waiting for wall-clock deadlines. Instance ids stay within the
    production ``^i-[0-9a-f]{8,17}$`` contract (enrollment requests and the AWS
    identity verifier both validate it), so the fake uses the hex-only
    ``i-fa<seq>`` form rather than a literal ``i-fake`` prefix.
    """

    offers: tuple[ComputeOffer, ...]
    clock: Callable[[], datetime] = utc_now
    _pools: dict[str, _FakePoolState] = field(default_factory=dict, init=False)
    _destroyed: set[str] = field(default_factory=set, init=False)
    _sequence: int = field(default=0, init=False)

    def list_offers(self) -> Iterable[ComputeOffer]:
        return self.offers

    def ensure_pool(self, request: ProviderPoolRequest) -> ProviderPoolSnapshot:
        pool = self._pool(request)
        pool.deleted = False
        self._apply_capacity(
            pool,
            desired_machines=request.desired_machines,
            max_machines=request.max_machines,
        )
        return self._snapshot(pool)

    def describe_pool(self, request: ProviderPoolRequest) -> ProviderPoolSnapshot:
        return self._snapshot(self._pool(request))

    def set_pool_capacity(
        self,
        request: ProviderPoolRequest,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> ProviderPoolSnapshot:
        pool = self._pool(request)
        pool.deleted = False
        self._apply_capacity(
            pool,
            desired_machines=desired_machines,
            max_machines=max_machines,
        )
        return self._snapshot(pool)

    def release_machine(
        self,
        request: ProviderPoolRequest,
        provider_instance_id: str,
    ) -> ProviderPoolSnapshot:
        pool = self._pool(request)
        if pool.instances.pop(provider_instance_id, None) is not None:
            self._destroyed.add(provider_instance_id)
            pool.desired_machines = max(pool.desired_machines - 1, 0)
        return self._snapshot(pool)

    def delete_pool(self, request: ProviderPoolRequest) -> ProviderPoolSnapshot:
        pool = self._pool(request)
        for instance_id in list(pool.instances):
            del pool.instances[instance_id]
            self._destroyed.add(instance_id)
        pool.desired_machines = 0
        pool.deleted = True
        return self._snapshot(pool)

    def machine_storage_destroyed(
        self,
        request: ProviderPoolRequest,
        provider_instance_id: str,
        storage_volume_ids: tuple[str, ...],
    ) -> bool:
        del storage_volume_ids
        pool = self._pool(request)
        if provider_instance_id in pool.instances:
            return False
        return provider_instance_id in self._destroyed

    def _pool(self, request: ProviderPoolRequest) -> _FakePoolState:
        return self._pools.setdefault(
            request.pool_id,
            _FakePoolState(
                resource_id=f"fake-{request.pool_id}",
                max_machines=request.max_machines,
            ),
        )

    def _apply_capacity(
        self,
        pool: _FakePoolState,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> None:
        pool.desired_machines = max(desired_machines, 0)
        pool.max_machines = max(max_machines, 0)
        while len(pool.instances) < pool.desired_machines:
            self._launch(pool)
        while len(pool.instances) > pool.desired_machines:
            newest = next(reversed(pool.instances))
            del pool.instances[newest]
            self._destroyed.add(newest)

    def _launch(self, pool: _FakePoolState) -> None:
        self._sequence += 1
        instance_id = f"i-fa{self._sequence:012x}"
        pool.instances[instance_id] = FakeInstance(
            instance_id=instance_id,
            storage_volume_ids=(f"vol-fa{self._sequence:012x}",),
            launched_at=self.clock(),
        )

    def _snapshot(self, pool: _FakePoolState) -> ProviderPoolSnapshot:
        observed = len(pool.instances)
        if pool.deleted:
            phase = ProviderCapacityPhase.Deleted
        elif observed == pool.desired_machines:
            phase = ProviderCapacityPhase.Ready
        else:
            phase = ProviderCapacityPhase.Provisioning
        return ProviderPoolSnapshot(
            phase=phase,
            resource_id=pool.resource_id,
            desired_machines=pool.desired_machines,
            max_machines=pool.max_machines,
            observed_machines=observed,
            instances=[
                ProviderPoolInstance(
                    provider_instance_id=instance.instance_id,
                    status=ProviderMachineStatus.Active,
                    storage_volume_ids=instance.storage_volume_ids,
                )
                for instance in pool.instances.values()
            ],
            provider_state=ComputePoolProviderState(resource_id=pool.resource_id),
        )


__all__ = ["FakeInstance", "FakePooledCapacityProvider"]
