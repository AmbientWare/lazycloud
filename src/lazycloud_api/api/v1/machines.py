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
from lazycloud_api.services import fly_app_manager, platform_manager
from lazycloud_api.services.fly.schemas import (
    FlyMachineConfig,
    FlyRegion,
)
from lazycloud_api.services.platform.schemas import PlatformOptions
from lazycloud_api.services.fly.utils import get_app_ipv4

machines_router = APIRouter(prefix="/machines", tags=["machines"])


@machines_router.get("")
async def get_machines(
    id: int | None = None,
    user_id: str | None = None,
    current_user: UserData = Depends(get_current_active_user),
) -> List[MachinePydantic]:
    filters: Dict[str, Any] = {
        "user_id": await check_user_id_request(user_id, current_user)
    }

    if id is not None:
        # add the id to the filters if it is provided
        filters["id"] = id

    """Get a list of machines"""
    machines = await db.machines.afind(filters=filters)

    return machines


@machines_router.get("/options")
async def get_platform_options(
    _=Depends(get_current_active_user),
) -> PlatformOptions:
    return await platform_manager.get_platform_options()


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

    ipv4 = await get_app_ipv4(usage_uuid)
    port = machine.app_port

    if ipv4 is None:
        raise HTTPException(status_code=500, detail="Machine has no IPv4")

    return MachineConnectionDetailsResponse(ip=ipv4, port=port)


class CreateMachineRequest(BaseModel):
    user_id: Optional[str] = None
    name: str
    public_key: str
    file_system_id: int
    region: FlyRegion = Field(default=FlyRegion.ORD)
    cpu: int = Field(default=1)
    memory: int = Field(default=1024)
    volume_size: int = Field(default=10)
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

    # get the file_system from our database by name
    file_system = await db.file_systems.afind_one(
        filters={
            "id": create_machine_request.file_system_id,
            "user_id": user_id,
        }
    )

    # make sure the file system exists
    if file_system is None or file_system.id is None:
        raise HTTPException(status_code=404, detail="File system not found")

    # create the machine in our database
    app_port = await db.machines.get_first_available_port(usage_uuid)
    new_machine = await db.machines.acreate(
        MachinePydantic(
            user_id=user_id,
            name=create_machine_request.name,
            region=create_machine_request.region.value,
            image=file_system.image,  # set the image to be the same as the file system
            cpu_kind="performance",  # NOTE: right now we only support performance machines, maybe shared in the future
            cpu=create_machine_request.cpu,
            memory=create_machine_request.memory,
            gpu_kind=create_machine_request.gpu_kind,
            status=MachineStatus.INITIALIZING,
            app_port=app_port,
            file_system_id=file_system.id,
        )
    )

    if new_machine is None or new_machine.id is None:
        raise HTTPException(status_code=500, detail="Failed to create machine")

    # now deploy the app with vm on fly
    try:
        machine_config = FlyMachineConfig(
            machine_id=new_machine.id,
            usage_uuid=usage_uuid,
            file_system_id=file_system.id,
            cpu_kind="performance",  # TODO: maybe make configurable in the future
            port=app_port,
            public_key=ssh_key.public_key,
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

        await fly_app_manager.create_machine(machine_config)

    except Exception as e:
        logger.error(f"Error creating machine: {e}\n Attempting to clean up machine.")
        try:
            # delete the machine from fly, dns record, and db
            await fly_app_manager.destroy_machine(usage_uuid, new_machine.id)
        except Exception as e:
            logger.error(f"Error deleting machine: {e}")

    finally:
        await fly_app_manager.clean(usage_uuid)

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

    # delete the machine from fly
    try:
        await fly_app_manager.destroy_machine(usage_uuid, machine.id)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # finally run cleanup
    await fly_app_manager.clean(usage_uuid)

    return machine
