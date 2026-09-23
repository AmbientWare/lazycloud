from __future__ import annotations

from uuid import uuid4

from database.context import ServiceContext
from database.repositories.compute import (
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.disks import DiskRepository
from database.repositories.orchestration import ContainerRepository, MachineRepository
from database.tables.disks import DiskTable
from database.tables.orchestration import ContainerTable
from shared.compute_fleet import Machine
from shared.compute_policy import ComputeUnitRecord, UnitName
from shared.containers import ContainerRecord, ContainerStatus
from shared.placement import Placement
from shared.timestamps import utc_now


def test_a_volume_counts_on_the_machine_it_is_on_unless_its_holder_runs_there(
    service_context: ServiceContext,
) -> None:
    with service_context.database.session() as session:
        workspace_id = service_context.default_workspace_id(session)
        pool = ComputeUnitRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            name=UnitName("disk-attachments"),
            placement=Placement.machine("disk-attachments"),
        )
        ComputeUnitRepository(session).upsert(pool)
        instances = ComputeProviderInstanceRepository(session)
        machines: dict[str, str] = {}
        for instance_id in ("i-a", "i-b"):
            machine_id = str(uuid4())
            MachineRepository(session).upsert(
                Machine(id=machine_id, placement=pool.placement, provider="aws"),
                workspace_id=workspace_id,
            )
            instances.upsert(
                ComputeProviderInstanceRecord(
                    id=str(uuid4()),
                    provider="aws",
                    offer_id="m7i.xlarge:us-east-2",
                    instance_id=instance_id,
                    status="running",
                    source="pooled",
                    pool_id=pool.id,
                )
            )
            assert instances.bind_machine(pool.id, instance_id, machine_id) is not None
            machines[instance_id] = machine_id

        holder = str(uuid4())
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=holder,
                name="box",
                image="image",
                command=[],
                workspace_id=workspace_id,
                status=ContainerStatus.Running,
            )
        )
        container = session.get(ContainerTable, holder)
        assert container is not None
        container.machine_id = machines["i-b"]

        def disk(name: str, *, state: str, instance_id: str) -> None:
            session.add(
                DiskTable(
                    workspace_id=workspace_id,
                    name=name,
                    size_bytes=1024**3,
                    status="attached",
                    holder_container_id=holder,
                    lease_token="lease",
                    volume_state=state,
                    volume_id=f"vol-{name}",
                    volume_provider_ref="aws:platform",
                    volume_capacity_workspace_id=workspace_id,
                    volume_region="us-east-2",
                    volume_zone="use2-az1",
                    volume_instance_id=instance_id,
                    volume_size_bytes=11 * 1024**3,
                    volume_token=name,
                    volume_driver="driver",
                    volume_changed_at=utc_now(),
                    volume_driven_at=utc_now(),
                )
            )

        # The holder moved to machine B while its old volume is still leaving A.
        disk("leaving-a", state="detaching", instance_id="i-a")
        disk("held-on-b", state="attached", instance_id="i-b")
        session.flush()

        counts = DiskRepository(session).unheld_volume_attachments(list(machines.values()))

    assert counts == {machines["i-a"]: 1}
