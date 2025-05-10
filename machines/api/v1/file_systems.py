from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from loguru import logger

from machines.api.security import (
    check_user_id_request,
    get_user_usage_uuid,
    get_current_active_user,
    UserData,
)
from machines.database import db
from machines.database.file_systems import FileSystemPydantic
from machines.services.platform.schemas import ImageTypes
from machines.services.fly.schemas import FlyRegion
from machines.services import fly_app_manager

file_systems_router = APIRouter(prefix="/file-systems", tags=["file-systems"])


@file_systems_router.get("")
async def get_file_systems(
    user_id: Optional[str] = None,
    id: Optional[int] = None,
    available: bool = False,
    current_user: UserData = Depends(get_current_active_user),
) -> List[FileSystemPydantic]:
    user_id = await check_user_id_request(user_id, current_user)

    filters: Dict[str, Any] = {"user_id": user_id}
    if id:
        # add the id to the filters if it is provided
        filters["id"] = id

    file_systems = await db.file_systems.afind(filters=filters)

    if available:
        # get all file systems that are not connected to a machine
        file_systems = await db.file_systems.afind(filters={"user_id": user_id})
        for file_system in file_systems.copy():
            machine = await db.machines.afind_one(
                filters={"file_system_id": file_system.id}
            )
            if machine:
                file_systems.remove(file_system)

    logger.info(f"Found {len(file_systems)} file systems")

    return file_systems


class CreateFileSystemRequest(BaseModel):
    user_id: Optional[str] = None
    gpu_kind: Optional[str] = None
    name: str
    size: int
    region: FlyRegion


@file_systems_router.post("")
async def create_file_system(
    request: CreateFileSystemRequest,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> FileSystemPydantic:
    user_id = await check_user_id_request(request.user_id, current_user)

    try:
        # first create the file system in our database
        file_system = await db.file_systems.acreate(
            FileSystemPydantic(
                user_id=user_id,
                name=request.name,
                size=request.size,
                region=request.region.value,
                # NOTE: maybe make this configurable?
                # For now we will manage the latest image version on our end.
                image=ImageTypes.UBUNTU_22_04.value,
            )
        )

        # create the volume on fly
        if file_system is not None and file_system.id is not None:
            await fly_app_manager.create_file_system(
                usage_uuid,
                file_system.id,
                request.size,
                request.region,
                request.gpu_kind,
            )

    except Exception as e:
        # delete the file system from our database
        if file_system is not None and file_system.id is not None:
            await db.file_systems.adelete(file_system.id)

        raise HTTPException(status_code=500, detail=str(e))

    # run cleanup
    await fly_app_manager.clean(usage_uuid)

    return file_system


class DuplicateFileSystemRequest(BaseModel):
    user_id: Optional[str] = None
    id: int
    duplicate_name: str


@file_systems_router.post("/duplicate")
async def duplicate_file_system(
    request: DuplicateFileSystemRequest,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> FileSystemPydantic:
    user_id = await check_user_id_request(request.user_id, current_user)

    name_already_exists = await db.file_systems.afind_one(
        filters={"name": request.duplicate_name, "user_id": user_id}
    )
    if name_already_exists:
        raise HTTPException(status_code=400, detail="File system name already exists")

    file_system = await db.file_systems.afind_one(
        filters={"id": request.id, "user_id": user_id}
    )
    if not file_system or file_system.id is None:
        raise HTTPException(status_code=404, detail="File system not found")

    try:
        new_file_system = await fly_app_manager.duplicate_file_system(
            usage_uuid, file_system, request.duplicate_name
        )

        return new_file_system

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ExtendFileSystemRequest(BaseModel):
    user_id: Optional[str] = None
    size: int
    id: int


@file_systems_router.put("")
async def extend_file_system(
    request: ExtendFileSystemRequest,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> FileSystemPydantic:
    user_id = await check_user_id_request(request.user_id, current_user)

    """Extend the volume of a file system"""
    file_system = await db.file_systems.afind_one(
        filters={"id": request.id, "user_id": user_id}
    )
    if not file_system or file_system.id is None:
        raise HTTPException(status_code=404, detail="File system not found")

    try:
        await fly_app_manager.extend_file_system(
            usage_uuid, file_system.id, request.size
        )
        file_system.size = request.size
        await db.file_systems.aupdate(file_system)

        return file_system

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class DeleteFileSystemRequest(BaseModel):
    id: int
    user_id: Optional[str] = None


@file_systems_router.delete("")
async def delete_file_systems(
    request: DeleteFileSystemRequest,
    current_user: UserData = Depends(get_current_active_user),
    usage_uuid: str = Depends(get_user_usage_uuid),
) -> FileSystemPydantic | None:
    user_id = await check_user_id_request(request.user_id, current_user)

    filters: Dict[str, Any] = {"user_id": user_id}
    if request.id:
        filters["id"] = request.id

    file_system = await db.file_systems.afind_one(filters=filters)

    if file_system is None or file_system.id is None:
        raise HTTPException(status_code=404, detail="File system not found")

    # make sure the fs is not connected to a machine
    machine = await db.machines.afind_one(filters={"file_system_id": file_system.id})
    if machine:
        raise HTTPException(
            status_code=400,
            detail="File system is connected to a machine. Destroy the machine first.",
        )

    # remove the volume from fly
    await fly_app_manager.destroy_file_system(usage_uuid, file_system.id)

    return file_system
