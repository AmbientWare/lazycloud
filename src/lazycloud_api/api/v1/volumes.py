from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from loguru import logger

from lazycloud_api.api.security import (
    check_user_id_request,
    get_user_usage_uuid,
    get_current_active_user,
    UserData,
)
from lazycloud_api.database import db
from lazycloud_api.database.volumes import VolumePydantic
from lazycloud_api.services.fly.schemas import FlyRegion
from lazycloud_api.services import fly_app_manager

volumes_router = APIRouter(prefix="/volumes", tags=["volumes"])


@volumes_router.get("")
async def get_volumes(
    user_id: Optional[str] = None,
    id: Optional[int] = None,
    machine_id: Optional[int] = None,
    current_user: UserData = Depends(get_current_active_user),
) -> List[VolumePydantic]:
    user_id = await check_user_id_request(user_id, current_user)

    filters: Dict[str, Any] = {"user_id": user_id}
    if id:
        # add the id to the filters if it is provided
        filters["id"] = id
        volumes = await db.volumes.afind(filters=filters)

    if machine_id:
        filters["machine_id"] = machine_id
        machine = await db.machines.afind_one(
            filters={"id": machine_id, "user_id": user_id}
        )

    if machine_id and id:
        raise HTTPException(
            status_code=400,
            detail="Cannot provide both machine_id and id",
        )

    # get all volumes that are not connected to a machine
    volumes = await db.volumes.afind(filters={"user_id": user_id})
    for volume in volumes.copy():
        machine = await db.machines.afind_one(
            filters={"volume_id": volume.id, "user_id": user_id}
        )
        if machine:
            volumes.remove(volume)

    logger.info(f"Found {len(volumes)} volumes")

    return volumes


class CreateVolumeRequest(BaseModel):
    user_id: Optional[str] = None
    gpu_kind: Optional[str] = None
    name: str
    size: int
    region: FlyRegion
    machine_id: int
    mount_path: str = "/data"


@volumes_router.post("")
async def create_volume(
    request: CreateVolumeRequest,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> VolumePydantic:
    user_id = await check_user_id_request(request.user_id, current_user)

    try:
        # first create the file system in our database
        volume = await db.volumes.acreate(
            VolumePydantic(
                user_id=user_id,
                name=request.name,
                size=request.size,
                region=request.region.value,
                mount_path=request.mount_path,
            )
        )

        # create the volume on fly
        if volume is not None and volume.id is not None:
            await fly_app_manager.create_volume(
                usage_uuid,
                volume.id,
                request.machine_id,
                request.size,
                request.region,
                request.gpu_kind,
            )

    except Exception as e:
        # delete the volume from our database
        if volume is not None and volume.id is not None:
            await db.volumes.adelete(volume.id)

        raise HTTPException(status_code=500, detail=str(e))

    return volume


class ExtendVolumeRequest(BaseModel):
    user_id: Optional[str] = None
    size: int
    machine_id: int
    mount_path: str = "/data"


@volumes_router.put("")
async def extend_volume(
    request: ExtendVolumeRequest,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> VolumePydantic:
    user_id = await check_user_id_request(request.user_id, current_user)

    """Extend the volume of a file system"""
    volume = await db.volumes.afind_one(
        filters={
            "machine_id": request.machine_id,
            "user_id": user_id,
            "mount_path": request.mount_path,
        }
    )
    if not volume or volume.id is None:
        raise HTTPException(status_code=404, detail="Volume not found")

    try:
        await fly_app_manager.extend_volume(
            usage_uuid, request.machine_id, volume.id, request.size
        )
        volume.size = request.size
        await db.volumes.aupdate(volume)

        return volume

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class DeleteVolumeRequest(BaseModel):
    id: int
    user_id: Optional[str] = None


@volumes_router.delete("")
async def delete_volumes(
    request: DeleteVolumeRequest,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> VolumePydantic | None:
    user_id = await check_user_id_request(request.user_id, current_user)

    filters: Dict[str, Any] = {"user_id": user_id}
    if request.id:
        filters["id"] = request.id

    volume = await db.volumes.afind_one(filters=filters)

    if volume is None or volume.id is None:
        raise HTTPException(status_code=404, detail="Volume not found")

    # make sure the volume is not connected to a machine
    machine = await db.machines.afind_one(filters={"volume_id": volume.id})

    if machine is None or machine.id is None:
        raise HTTPException(
            status_code=400,
            detail="No machine found for volume",
        )

    # remove the volume from fly
    await fly_app_manager.destroy_volume(usage_uuid, machine.id, volume.id)

    return volume
