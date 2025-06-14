from fastapi import APIRouter, HTTPException, Depends
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from loguru import logger

from lazycloud_api.api.security import (
    get_current_active_user,
    UserData,
    get_user_usage_uuid,
    check_user_id_request,
)
from lazycloud_api.database import db
from lazycloud_api.database.machines import MachinePydantic, MachineStatus
from lazycloud_api.services import fly_app_manager
from lazycloud_api.services.fly.schemas import (
    FlyMachineConfig,
    FlyRegion,
)
from lazycloud_api.services.fly.utils import get_app_ipv4

machines_router = APIRouter(prefix="/machines", tags=["machines"])


class GetMachinesResponse(BaseModel):
    id: int
    name: str
    status: MachineStatus
    region: FlyRegion
    cpu: int
    memory: int
    disk_size: int
    gpu_kind: str | None


@machines_router.get("")
async def get_machines(
    id: int | None = None,
    user_id: str | None = None,
    current_user: UserData = Depends(get_current_active_user),
) -> List[GetMachinesResponse]:
    filters: Dict[str, Any] = {
        "user_id": await check_user_id_request(user_id, current_user)
    }

    if id is not None:
        # add the id to the filters if it is provided
        filters["id"] = id

    """Get a list of machines"""
    machines = await db.machines.afind(filters=filters)

    response_data = []
    for machine in machines:
        if machine.id is None:
            continue

        volume = await db.volumes.afind_one(filters={"machine_id": machine.id})
        if volume is None:
            continue

        response_data.append(
            GetMachinesResponse(
                id=machine.id,
                name=machine.name,
                status=machine.status,
                region=FlyRegion(machine.region),
                cpu=machine.cpu,
                memory=machine.memory,
                disk_size=volume.size,
                gpu_kind=machine.gpu_kind,
            )
        )

    return response_data


class MachineConnectionDetailsResponse(BaseModel):
    ip: str
    port: int


@machines_router.get("/{id}/connection-details")
async def get_machines_connection_details(
    id: int,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> MachineConnectionDetailsResponse:
    """Get a list of machines"""
    # make sure the machine exists
    machine = await db.machines.afind_one(
        filters={"id": id, "user_id": current_user.user_id}
    )
    if machine is None or machine.id is None:
        raise HTTPException(status_code=404, detail="Machine not found")

    ipv4 = await get_app_ipv4(usage_uuid, machine.id)
    port = machine.app_port

    if ipv4 is None:
        raise HTTPException(status_code=500, detail="Machine has no IPv4")

    return MachineConnectionDetailsResponse(ip=ipv4, port=port)


class CreateMachineRequest(BaseModel):
    user_id: Optional[str] = None
    name: str
    public_key: str
    region: FlyRegion = Field(default=FlyRegion.ORD)
    cpu: int = Field(default=1)
    memory: int = Field(default=1024)
    disk_size: int = Field(default=10)
    gpu_kind: str | None = Field(default=None)


@machines_router.post("")
async def create_machine(
    create_machine_request: CreateMachineRequest,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
):
    user_id = await check_user_id_request(create_machine_request.user_id, current_user)

    # make sure the machine name is unique
    found = await db.machines.afind_one(
        filters={
            "name": create_machine_request.name,
            "user_id": user_id,
        }
    )
    if found:
        raise HTTPException(
            status_code=400,
            detail="Machine name already exists. Please choose a different name.",
        )

    # get the ssh key from the database
    ssh_key = await db.ssh_keys.afind_one(
        filters={
            "name": create_machine_request.public_key,
            "user_id": user_id,
        }
    )
    if ssh_key is None:
        raise HTTPException(status_code=400, detail="SSH key not found")

    # now deploy the app with vm on fly
    try:
        machine_config = FlyMachineConfig(
            usage_uuid=usage_uuid,
            cpu_kind="performance",  # TODO: maybe make configurable in the future
            public_key=ssh_key.public_key,
            volume_size=create_machine_request.disk_size,
        )

        # Only set optional fields if they are provided
        if create_machine_request.cpu is not None:
            machine_config.cpu = create_machine_request.cpu
        if create_machine_request.memory is not None:
            machine_config.memory = create_machine_request.memory
        if create_machine_request.region is not None:
            machine_config.region = create_machine_request.region
        if create_machine_request.gpu_kind is not None:
            machine_config.gpu_kind = create_machine_request.gpu_kind

        new_machine = await fly_app_manager.create_machine(
            user_id, create_machine_request.name, machine_config
        )

    except Exception as e:
        logger.error(f"Error creating machine: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return new_machine


class RestartMachineRequest(BaseModel):
    user_id: Optional[str] = None


@machines_router.post("/{id}/restart")
async def restart_machine(
    id: int,
    restart_machine_request: RestartMachineRequest,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> bool:
    user_id = await check_user_id_request(restart_machine_request.user_id, current_user)

    # make sure the machine exists and belongs to the user
    machine = await db.machines.afind_one(filters={"id": id, "user_id": user_id})
    if machine is None or machine.id is None:
        raise HTTPException(status_code=404, detail="Machine not found")

    try:
        await fly_app_manager.restart_machine(usage_uuid, machine.id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return True


class EnableMachineAutoStopRequest(BaseModel):
    enabled: bool
    user_id: Optional[str] = None


@machines_router.post("/{id}/auto-stop")
async def enable_machine_auto_stop(
    id: int,
    enable_machine_auto_stop_request: EnableMachineAutoStopRequest,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> bool:
    user_id = await check_user_id_request(
        enable_machine_auto_stop_request.user_id, current_user
    )

    # make sure the machine exists and belongs to the user
    machine = await db.machines.afind_one(filters={"id": id, "user_id": user_id})
    if machine is None or machine.id is None:
        raise HTTPException(status_code=404, detail="Machine not found")

    try:
        await fly_app_manager.auto_stop(
            usage_uuid, id, enable_machine_auto_stop_request.enabled
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return True


class ScaleMachineRequest(BaseModel):
    user_id: Optional[str] = None
    cpu_kind: str | None = None
    cpu: int | None = None
    memory: int | None = None
    region: FlyRegion | None = None


@machines_router.put("/{id}")
async def scale_machine(
    id: int,
    scale_machine_request: ScaleMachineRequest,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
):
    user_id = await check_user_id_request(scale_machine_request.user_id, current_user)

    try:
        machine = await db.machines.afind_one(filters={"id": id, "user_id": user_id})
        if machine is None or machine.id is None:
            raise HTTPException(status_code=404, detail="Machine not found")

        if machine.user_id != user_id:
            raise HTTPException(status_code=403, detail="Machine not found")

        print(f"Scaling machine with new values: {scale_machine_request}")

        if scale_machine_request.cpu_kind is not None:
            machine.cpu_kind = scale_machine_request.cpu_kind
        if scale_machine_request.cpu is not None:
            machine.cpu = scale_machine_request.cpu
        if scale_machine_request.memory is not None:
            machine.memory = scale_machine_request.memory
        if scale_machine_request.region is not None:
            machine.region = scale_machine_request.region.value

        # scale the app on fly
        await fly_app_manager.scale_machine(
            usage_uuid,
            machine.id,
            machine.cpu_kind,
            machine.cpu,
            machine.memory,
        )

        # update the machine in our database
        return await db.machines.aupdate(machine)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@machines_router.delete("")
async def delete_machine(
    id: int,
    user_id: str | None = None,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> MachinePydantic | None:
    user_id = await check_user_id_request(user_id, current_user)

    # get machine from db
    machine = await db.machines.afind_one(filters={"user_id": user_id, "id": id})
    if machine is None or machine.id is None:
        return None

    # destroying the app will also destroy the machine and volume
    try:
        await fly_app_manager.destroy_app(usage_uuid, machine.id)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return machine
