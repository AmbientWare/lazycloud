from fastapi import APIRouter, HTTPException, Depends
from typing import List
from pydantic import BaseModel, Field

from machines.api.security import get_current_active_user, UserData
from machines.database import db
from machines.database.machines import MachinePydantic, MachineStatus
from machines.fly import app_manager
from machines.fly.schemas import (
    AppConfig,
    FlyMachineConfig,
    FlyRegion,
    ImageTypes,
    FlyCommandError,
)

machines_router = APIRouter(prefix="/machines", tags=["machines"])


@machines_router.get("")
async def get_machines(
    machine_name: str | None = None,
    current_user: UserData = Depends(get_current_active_user),
) -> List[MachinePydantic]:
    """Get a list of machines"""
    if machine_name is None:
        machines = await db.machines.afind(filters={"user_id": current_user.user_id})
    else:
        machine = await db.machines.afind_one(
            filters={"name": machine_name, "user_id": current_user.user_id}
        )
        machines = [machine] if machine else []

    return machines


class MachineAliasResponse(BaseModel):
    alias: str
    port: int


@machines_router.get("/alias/{machine_name}")
async def get_machines_alias(
    machine_name: str,
    current_user: UserData = Depends(get_current_active_user),
) -> MachineAliasResponse:
    """Get a list of machines"""
    # make sure the machine exists
    machine = await db.machines.afind_one(
        filters={"name": machine_name, "user_id": current_user.user_id}
    )
    if machine is None:
        raise HTTPException(status_code=404, detail="Machine not found")

    alias = f"{machine.name}-{machine.user_id}.fly.dev"

    return MachineAliasResponse(alias=alias, port=10022)


class CreateMachineRequest(BaseModel):
    name: str
    public_key: str
    region: FlyRegion = Field(default=FlyRegion.LAX)
    image: ImageTypes = Field(default=ImageTypes.UBUNTU_22_04)
    cpu_kind: str = Field(default="shared")
    cpu: int = Field(default=1)
    memory: int = Field(default=1024)
    volume_size: int = Field(default=10)


@machines_router.post("")
async def create_machine(
    create_machine_request: CreateMachineRequest,
    current_user: UserData = Depends(get_current_active_user),
):
    # make sure the machine name is unique
    found = await db.machines.afind_one(
        filters={"name": create_machine_request.name, "user_id": current_user.user_id}
    )
    if found:
        raise HTTPException(status_code=400, detail="Machine name already exists")

    app_config = AppConfig(
        user_id=current_user.user_id,
        name=create_machine_request.name,
        public_key=create_machine_request.public_key,
    )

    created_on_fly = False
    try:
        # first create the app on fly
        await app_manager.create_app(app_config)
        created_on_fly = True

        # now deploy the app with vm on fly
        machine_config = FlyMachineConfig(
            name=create_machine_request.name,
        )

        # Only set optional fields if they are provided
        if create_machine_request.cpu_kind is not None:
            machine_config.cpu_kind = create_machine_request.cpu_kind
        if create_machine_request.cpu is not None:
            machine_config.cpu = create_machine_request.cpu
        if create_machine_request.memory is not None:
            machine_config.memory = create_machine_request.memory
        if create_machine_request.volume_size is not None:
            machine_config.initial_volume_size = create_machine_request.volume_size
        if create_machine_request.region is not None:
            machine_config.region = create_machine_request.region

        await app_manager.deploy_app(machine_config, current_user.user_id)

        # finally create the machine in our database
        entity = await db.machines.acreate(
            MachinePydantic(
                user_id=current_user.user_id,
                name=create_machine_request.name,
                region=create_machine_request.region.value,
                image=create_machine_request.image.value,
                cpu_kind=create_machine_request.cpu_kind,
                cpu=create_machine_request.cpu,
                memory=create_machine_request.memory,
                volume_size=create_machine_request.volume_size,
                status=MachineStatus.DEPLOYED,
            )
        )

    except Exception as e:
        print(f"Error creating machine: {e}")
        if created_on_fly:
            await app_manager.delete_app(
                create_machine_request.name, current_user.user_id
            )
        raise HTTPException(status_code=500, detail=str(e))

    return entity


@machines_router.post("/{machine_name}/volumes")
async def extend_volume(
    machine_name: str,
    volume_size: int,
    current_user: UserData = Depends(get_current_active_user),
) -> MachinePydantic:
    """Extend the volume of a machine"""
    machine = await db.machines.afind_one(
        filters={"name": machine_name, "user_id": current_user.user_id}
    )
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    try:
        await app_manager.extend_volume(machine_name, current_user.user_id, volume_size)
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
):
    try:
        machine = await db.machines.afind_one(
            filters={"name": machine_name, "user_id": current_user.user_id}
        )
        if machine is None:
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
        await app_manager.scale_app(
            machine_name,
            current_user.user_id,
            machine.cpu_kind,
            machine.cpu,
            machine.memory,
        )

        # update the machine in our database
        return await db.machines.aupdate(machine)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@machines_router.delete("/{machine_name}")
async def delete_machine(
    machine_name: str,
    current_user: UserData = Depends(get_current_active_user),
):
    try:
        # get machine from db
        machine = await db.machines.afind_one(
            filters={"name": machine_name, "user_id": current_user.user_id}
        )
        if machine is None or machine.id is None:
            raise HTTPException(status_code=404, detail="Machine not found")

        if machine.user_id != current_user.user_id:
            raise HTTPException(status_code=403, detail="Machine not found")

        # delete the machine from fly
        try:
            await app_manager.delete_app(machine.name, current_user.user_id)
        except Exception as e:
            print(f"Error deleting machine from fly: {e}")
            # raise HTTPException(status_code=500, detail=str(e))

        # delete the machine from db
        try:
            await db.machines.adelete(machine.id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
