import asyncio
from typing import Optional
from loguru import logger

from lazycloud_api.celery_app import app
from lazycloud_api.services import fly_app_manager
from lazycloud_api.services.fly.schemas import FlyRegion


@app.task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,  # 5 minutes max
    max_retries=3,
)
def create_volume_task(
    usage_uuid: str,
    volume_id: int,
    machine_id: int,
    size: int,
    region: FlyRegion,
    gpu_kind: Optional[str] = None,
):

    asyncio.run(
        fly_app_manager.create_volume(
            usage_uuid, volume_id, machine_id, size, region, gpu_kind
        )
    )

    logger.info(f"Successfully created volume {volume_id} for machine {machine_id}")
    return {"status": "success", "volume_id": volume_id, "machine_id": machine_id}


@app.task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,  # 5 minutes max
    max_retries=3,
)
def extend_volume_task(usage_uuid: str, machine_id: int, volume_id: int, new_size: int):
    """Extend a volume in the background"""
    asyncio.run(
        fly_app_manager.extend_volume(usage_uuid, machine_id, volume_id, new_size)
    )

    logger.info(f"Successfully extended volume {volume_id} to {new_size}GB")
    return {"status": "success", "volume_id": volume_id, "new_size": new_size}


@app.task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,  # 5 minutes max
    max_retries=3,
)
def delete_volume_task(usage_uuid: str, machine_id: int, volume_id: int):
    """Delete a volume in the background"""
    result = asyncio.run(
        fly_app_manager.destroy_volume(usage_uuid, machine_id, volume_id)
    )

    logger.info(f"Successfully deleted volume {volume_id}")
    return {"status": "success", "volume_id": volume_id, "result": result}
