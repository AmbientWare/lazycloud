from fastapi import APIRouter, HTTPException, Depends
from typing import List, Optional, Dict
from pydantic import BaseModel, Field

from machines.api.security import get_current_active_user, UserData, require_admin, get_user_usage_uuid
from machines.database import db
from machines.database.machines import MachinePydantic, MachineStatus
from machines.services import fly_app_manager, platform_manager, route_53
from machines.services.fly.schemas import (
    AppConfig,
    FlyMachineConfig,
    FlyRegion,
    ImageTypes,
    FlyCommandError,
)
from machines.services.platform.schemas import PlatformOptions

machines_router = APIRouter(prefix="/machines", tags=["machines"])


@machines_router.get("")
async def get_machines(
    user_id: str | None = None,
    machine_id: str | None = None,
    machine_name: str | None = None,
    current_user: UserData = Depends(get_current_active_user),
) -> List[MachinePydantic]:
    filters = {}
    if user_id is not None:
        if not await require_admin(current_user):
            raise HTTPException(
                status_code=403, detail="Only admins can access other users' machines"
            )
        filters["user_id"] = user_id
    else:
        filters["user_id"] = current_user.user_id

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

    app_name = await fly_app_manager.get_app_name(usage_uuid, machine.id)
    alias = route_53.get_cname_domain(app_name)

    return MachineAliasResponse(alias=alias, port=10022)


class CreateMachineRequest(BaseModel):
    user_id: Optional[str] = None
    name: str
    public_key: str
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
    if create_machine_request.user_id:
        if not require_admin(current_user):
            raise HTTPException(
                status_code=403,
                detail="Only admins can create machines for other users",
            )
        user_id = create_machine_request.user_id
    else:
        user_id = current_user.user_id

    # make sure the machine name is unique
    found = await db.machines.afind_one(
        filters={
            "name": create_machine_request.name,
            "user_id": user_id,
        }
    )
    if found:
        raise HTTPException(status_code=400, detail="Machine name already exists")

    # get the ssh key from the database
    ssh_key = await db.ssh_keys.afind_one(
        filters={
            "name": create_machine_request.public_key,
            "user_id": user_id,
        }
    )
    if ssh_key is None:
        raise HTTPException(status_code=400, detail="SSH key not found")

    # create the machine in our database
    new_machine = await db.machines.acreate(
        MachinePydantic(
            user_id=user_id,
            name=create_machine_request.name,
            region=create_machine_request.region.value,
            image=create_machine_request.image.value,
            cpu_kind="performance",  # TODO: maybe make configurable in the future
            cpu=create_machine_request.cpu,
            memory=create_machine_request.memory,
            volume_size=create_machine_request.volume_size,
            gpu_kind=create_machine_request.gpu_kind,
            status=MachineStatus.INITIALIZING,
        )
    )

    if (
        new_machine is None
        or new_machine.id is None
    ):
        raise HTTPException(status_code=500, detail="Failed to create machine")

    app_config = AppConfig(
        user_id=user_id,
        machine_id=new_machine.id,
        usage_uuid=usage_uuid,
        public_key=ssh_key.public_key,
    )

    # create the app on fly
    try:
        await fly_app_manager.create_app(app_config)

    except Exception as e:
        # only delete the machine from db, at this point it is not created on fly
        await db.machines.adelete(new_machine.id)
        raise HTTPException(status_code=500, detail=str(e))

    # now deploy the app with vm on fly
    try:
        machine_config = FlyMachineConfig(
            machine_id=new_machine.id,
            usage_uuid=usage_uuid,
            cpu_kind="performance",  # TODO: maybe make configurable in the future
        )

        # Only set optional fields if they are provided
        if create_machine_request.cpu is not None:
            machine_config.cpu = create_machine_request.cpu
        if create_machine_request.memory is not None:
            machine_config.memory = create_machine_request.memory
        if create_machine_request.volume_size is not None:
            machine_config.initial_volume_size = create_machine_request.volume_size
        if create_machine_request.region is not None:
            machine_config.region = create_machine_request.region
        if create_machine_request.gpu_kind is not None:
            machine_config.gpu_kind = create_machine_request.gpu_kind

        await fly_app_manager.deploy_app(machine_config)

    except Exception as e:
        # delete the machine from fly, dns record, and db
        await fly_app_manager.delete_app(usage_uuid, new_machine.id)

        raise HTTPException(status_code=500, detail=str(e))

    return new_machine


@machines_router.post("/{machine_name}/volumes")
async def extend_volume(
    machine_name: str,
    volume_size: int,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> MachinePydantic:
    """Extend the volume of a machine"""
    machine = await db.machines.afind_one(
        filters={"name": machine_name, "user_id": current_user.user_id}
    )
    if not machine or machine.id is None:
        raise HTTPException(status_code=404, detail="Machine not found")

    try:
        await fly_app_manager.extend_volume(usage_uuid, machine.id, volume_size)
        machine.volume_size = volume_size
        await db.machines.aupdate(machine)
        return machine

    except FlyCommandError as e:
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ScaleMachineRequest(BaseModel):
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
    try:
        machine = await db.machines.afind_one(
            filters={"name": machine_name, "user_id": current_user.user_id}
        )
        if machine is None or machine.id is None:
            raise HTTPException(status_code=404, detail="Machine not found")

        if machine.user_id != current_user.user_id:
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
        await fly_app_manager.scale_app(
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
    machine_id: str | None = None,
    machine_name: str | None = None,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> MachinePydantic | None:
    filters = {"user_id": current_user.user_id}
    if machine_id is not None:
        filters["id"] = machine_id
    if machine_name is not None:
        filters["name"] = machine_name

    # get machine from db
    machine = await db.machines.afind_one(filters=filters)
    if machine is None or machine.id is None:
        return None

    if machine.user_id != current_user.user_id:
        return None

    # delete the machine from fly
    try:
        await fly_app_manager.delete_app(usage_uuid, machine.id)

    except Exception as e:
        print(f"Error deleting machine from fly: {e}")
        # raise HTTPException(status_code=500, detail=str(e))

    # delete the machine from db
    try:
        await db.machines.adelete(machine.id)
        return machine

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
