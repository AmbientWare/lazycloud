"""Deployment-scoped capacity operations and supplier cost inspection."""

from __future__ import annotations

import time
from typing import Annotated

import typer
from compute.fleet_operations import FleetOperations
from compute.reserve_state import RedisFleetReserveState
from compute.service import ComputeService
from compute.supplier_costs import SupplierCostInspectionService
from coordination.redis_client import RedisClient, RedisSettings
from lazycloud._terminal.streams import console
from lazycloud.cli.components.results import emit_result
from shared.compute_policy import ComputeUnitRecord
from shared.contracts import ContractModel
from shared.errors import DomainError

from cli.platform_compute import platform_compute
from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

fleet_app = typer.Typer(
    help="Inspect, stop, resume and remove this deployment's platform capacity."
)


@fleet_app.command("clear-plan")
def fleet_clear_plan(ctx: typer.Context) -> None:
    """Clear the published fleet decision so the scheduler recomputes it."""
    redis = RedisClient.from_settings(RedisSettings())
    try:
        RedisFleetReserveState(redis).clear_publication()
    finally:
        redis.close()
    emit_result(
        ctx,
        payload={"cleared": True},
        title="Fleet plan cleared",
        fields={"next step": "The scheduler will publish a fresh plan."},
        tone="info",
    )


@fleet_app.command("stop-machine")
def fleet_stop_machine(
    ctx: typer.Context,
    machine_id: str,
    unit_id: Annotated[str, typer.Option("--unit-id", help="Machine's platform unit ID.")],
    confirm_stopped: Annotated[
        bool,
        typer.Option(
            "--confirm-stopped", help="Confirm workload admission and schedulers are stopped."
        ),
    ] = False,
) -> None:
    """Request a plain stop after the idle machine completes tenant cleanup."""
    if not confirm_stopped:
        raise typer.BadParameter(
            "Stop workload admission and schedulers, then pass --confirm-stopped"
        )
    with platform_compute() as compute:
        unit = FleetOperations(compute).stop_machine(unit_id=unit_id, machine_id=machine_id)
    emit_result(
        ctx,
        payload={
            "unit_id": unit.id,
            "machine_id": machine_id,
            "desired_machines": unit.desired_machines,
            "stopped_target": unit.stopped_machines,
        },
        title="Machine stop requested",
        fields={
            "unit": unit.id,
            "machine": machine_id,
            "completion": "Keep the control plane and gateway running until the machine stops.",
        },
        tone="info",
    )


@fleet_app.command("resume")
def fleet_resume(
    ctx: typer.Context,
    unit_id: str,
    desired: Annotated[
        int, typer.Option("--desired", min=1, help="Total running capacity after resume.")
    ],
    confirm_stopped: Annotated[
        bool,
        typer.Option(
            "--confirm-stopped", help="Confirm workload admission and schedulers are stopped."
        ),
    ] = False,
) -> None:
    """Restore a running target after checking this unit's stopped inventory.

    Normal capacity recovery can replace machines lost after this check.
    """
    if not confirm_stopped:
        raise typer.BadParameter(
            "Stop workload admission and schedulers, then pass --confirm-stopped"
        )
    with platform_compute() as compute:
        unit = FleetOperations(compute).resume_unit(unit_id=unit_id, desired=desired)
    emit_result(
        ctx,
        payload={
            "unit_id": unit.id,
            "desired_machines": unit.desired_machines,
            "stopped_target": unit.stopped_machines,
        },
        title="Fleet resume requested",
        fields={
            "unit": unit.id,
            "desired running": unit.desired_machines,
            "completion": "Verify worker intake before restoring workload admission.",
        },
        tone="info",
    )


@fleet_app.command("costs")
def fleet_costs(
    ctx: typer.Context,
    workspace_id: Annotated[str, typer.Option("--workspace-id", help="Unit's workspace ID.")],
    unit_id: Annotated[str, typer.Option("--unit-id", help="Provisioning unit ID.")],
) -> None:
    """Inspect saved supplier estimates and unknown costs without refreshing the catalog."""
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Admin).direct()
    )
    try:
        report = SupplierCostInspectionService(database).inspect(
            workspace_id=workspace_id, unit_id=unit_id
        )
    finally:
        database.dispose()
    emit_result(
        ctx,
        payload=report.model_dump(mode="json"),
        title="Recorded supplier cost estimates",
        fields={
            "unit": report.unit_id,
            "provider": report.provider,
            "offer known hourly USD micros": report.offer.known_hourly_micros,
            "offer complete hourly USD micros": report.offer.complete_hourly_micros,
            "nodes": [node.model_dump(mode="json") for node in report.nodes],
        },
        tone="info",
    )


class FleetUnitTeardown(ContractModel):
    unit_id: str
    name: str
    deleted: bool
    attempts: int
    reason: str = ""


@fleet_app.command("destroy")
def fleet_destroy(
    ctx: typer.Context,
    confirm_stopped: Annotated[
        bool,
        typer.Option(
            "--confirm-stopped", help="Confirm workload admission and schedulers are stopped."
        ),
    ] = False,
    timeout_seconds: Annotated[float, typer.Option("--timeout", min=1)] = 600,
    interval_seconds: Annotated[float, typer.Option("--interval", min=1, max=60)] = 5,
) -> None:
    """Remove platform units through their provider lifecycle. Customer capacity is excluded."""
    if not confirm_stopped:
        raise typer.BadParameter(
            "Stop workload admission and schedulers, then pass --confirm-stopped"
        )
    with platform_compute() as compute:
        units = compute.platform_units()
        for unit in units:
            console.print(
                f"Platform unit {unit.id}: {unit.name} ({unit.provider_ref}, {unit.region})"
            )
        outcomes = [
            _destroy_unit(
                compute, unit, timeout_seconds=timeout_seconds, interval_seconds=interval_seconds
            )
            for unit in units
        ]
    remaining = sum(not outcome.deleted for outcome in outcomes)
    emit_result(
        ctx,
        payload={
            "units": [outcome.model_dump(mode="json") for outcome in outcomes],
            "remaining": remaining,
        },
        title="Fleet teardown incomplete" if remaining else "Fleet removed",
        fields={"removed": len(outcomes) - remaining, "remaining": remaining},
        tone="warning" if remaining else "success",
    )
    if remaining:
        raise typer.Exit(code=1)


def _destroy_unit(
    compute: ComputeService,
    unit: ComputeUnitRecord,
    *,
    timeout_seconds: float,
    interval_seconds: float,
) -> FleetUnitTeardown:
    deadline = time.monotonic() + timeout_seconds
    attempts = 0
    while True:
        attempts += 1
        try:
            compute.delete_platform_unit(unit.capacity_owner_id)
        except DomainError as error:
            reason = error.message
            console.print(f"{unit.name}: {error.code}: {reason}", markup=False)
            retryable = error.code in {
                "capacity_reservation_lock_contended",
                "upstream_unavailable",
            }
            if not retryable or time.monotonic() + interval_seconds >= deadline:
                return FleetUnitTeardown(
                    unit_id=unit.id, name=unit.name, deleted=False, attempts=attempts, reason=reason
                )
            time.sleep(interval_seconds)
        else:
            console.print(f"{unit.name}: provider capacity removed", markup=False)
            return FleetUnitTeardown(
                unit_id=unit.id, name=unit.name, deleted=True, attempts=attempts
            )


__all__ = ["fleet_app"]
