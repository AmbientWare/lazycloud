from datetime import UTC, datetime, timedelta

from compute.fleet_policy import FleetCapacityPolicy
from compute.offers import ComputeOffer
from compute.providers import next_billing_renewal
from compute.warm_capacity import warm_capacity_target
from database.repositories.compute import PlatformCpuArrival


def test_provider_paid_hour_advances_only_after_its_boundary() -> None:
    started = datetime(2026, 9, 1, tzinfo=UTC)
    assert next_billing_renewal(
        started_at=started,
        minimum_seconds=3600,
        quantum_seconds=3600,
        now=started + timedelta(minutes=5),
    ) == (started + timedelta(hours=1))
    assert next_billing_renewal(
        started_at=started,
        minimum_seconds=3600,
        quantum_seconds=3600,
        now=started + timedelta(hours=1),
    ) == (started + timedelta(hours=2))


def test_warm_capacity_absorbs_launch_window_demand_and_shrinks_gradually() -> None:
    now = datetime(2026, 9, 1, 1, tzinfo=UTC)
    policy = FleetCapacityPolicy()
    offer = ComputeOffer(
        id="ccx23",
        provider="hetzner:platform",
        instance_type="ccx23",
        region="ash",
        cpu_millicores=4000,
        memory_mb=16384,
    )
    arrivals = [
        PlatformCpuArrival(now - timedelta(seconds=offset), 2000, 1024) for offset in (10, 20, 30)
    ]
    increased = warm_capacity_target(
        policy,
        offer,
        arrivals,
        (),
        preemptible=True,
        current=1,
        lower_since=None,
        now=now,
    )
    assert increased.machines == 2
    held = warm_capacity_target(
        policy,
        offer,
        (),
        (),
        preemptible=True,
        current=2,
        lower_since=None,
        now=now,
    )
    assert held.machines == 2
    reduced = warm_capacity_target(
        policy,
        offer,
        (),
        (),
        preemptible=True,
        current=2,
        lower_since=held.lower_since,
        now=now + timedelta(seconds=policy.warm_decrease_after_seconds),
    )
    assert reduced.machines == 1


def test_warm_capacity_counts_only_fitting_requests_using_reserved_memory() -> None:
    now = datetime(2026, 9, 1, 1, tzinfo=UTC)
    target = warm_capacity_target(
        FleetCapacityPolicy(),
        ComputeOffer(
            id="memory-bound-node",
            provider="hetzner:platform",
            instance_type="memory-bound-node",
            region="ash",
            cpu_millicores=4000,
            memory_mb=4096,
        ),
        [
            PlatformCpuArrival(now, cpu_millicores=500, reserved_memory_mib=3500),
            PlatformCpuArrival(now, cpu_millicores=4500, reserved_memory_mib=500),
            PlatformCpuArrival(now, cpu_millicores=500, reserved_memory_mib=5000),
        ],
        (),
        preemptible=True,
        current=1,
        lower_since=None,
        now=now,
    )
    assert target.machines == 1
