from fastapi import APIRouter, HTTPException, Depends
from typing import List, Optional
from pydantic import BaseModel, Field
from loguru import logger

from machines.api.security import (
    get_current_active_user,
    UserData,
    get_user_usage_uuid,
    check_user_id_request,
)
from machines.database import db
from machines.database.machines import MachinePydantic, MachineStatus
from machines.services import fly_app_manager, platform_manager, route_53
from machines.services.fly.schemas import (
    AppConfig,
    FlyMachineConfig,
    FlyRegion,
    ImageTypes,
)
from machines.database.file_systems import FileSystemPydantic
from machines.services.platform.schemas import PlatformOptions
from machines.services.fly.utils import get_app_name

machines_router = APIRouter(prefix="/machines", tags=["machines"])


@machines_router.get("")
async def get_machines(
    user_id: str | None = None,
    machine_id: str | None = None,
    machine_name: str | None = None,
    current_user: UserData = Depends(get_current_active_user),
) -> List[MachinePydantic]:
    filters = {"user_id": await check_user_id_request(user_id, current_user)}

    if machine_id is not None:
        filters["id"] = machine_id
    if machine_name is not None:
        filters["name"] = machine_name

    """Get a list of machines"""
    machines = await db.machines.afind(filters=filters)

    return machines


@machines_router.get("/options")
async def get_platform_options(
    _=Depends(get_current_active_user),
) -> PlatformOptions:
    return await platform_manager.get_platform_options()


class MachineAliasResponse(BaseModel):
    alias: str
    port: int


@machines_router.get("/alias/{machine_name}")
async def get_machines_alias(
    machine_name: str,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> MachineAliasResponse:
    """Get a list of machines"""
    # make sure the machine exists
    machine = await db.machines.afind_one(
        filters={"name": machine_name, "user_id": current_user.user_id}
    )
    if machine is None or machine.id is None:
        raise HTTPException(status_code=404, detail="Machine not found")

    app_name = await get_app_name(usage_uuid)
    alias = route_53.get_cname_domain(app_name)
    port = machine.app_port

    return MachineAliasResponse(alias=alias, port=port)


class CreateMachineRequest(BaseModel):
    user_id: Optional[str] = None
    name: str
    public_key: str
    file_system_id: int
    region: FlyRegion = Field(default=FlyRegion.LAX)
    image: ImageTypes = Field(default=ImageTypes.UBUNTU_22_04)
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
            image=create_machine_request.image.value,
            cpu_kind="performance",  # TODO: maybe make configurable in the future
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


class ScaleMachineRequest(BaseModel):
    user_id: Optional[str] = None
    cpu_kind: str | None = None
    cpu: int | None = None
    memory: int | None = None
    region: FlyRegion | None = None


@machines_router.put("/{machine_name}")
async def scale_machine(
    machine_name: str,
    scale_machine_request: ScaleMachineRequest,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
):
    user_id = await check_user_id_request(scale_machine_request.user_id, current_user)

    try:
        machine = await db.machines.afind_one(
            filters={"name": machine_name, "user_id": user_id}
        )
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
    user_id: str | None = None,
    machine_id: str | None = None,
    machine_name: str | None = None,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> MachinePydantic | None:
    user_id = await check_user_id_request(user_id, current_user)

    filters = {"user_id": user_id}
    if machine_id is not None:
        filters["id"] = machine_id
    if machine_name is not None:
        filters["name"] = machine_name

    # get machine from db
    machine = await db.machines.afind_one(filters=filters)
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
