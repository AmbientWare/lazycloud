import asyncio
from typing import Dict, Any
from loguru import logger

from lazycloud_api.celery_app import app
from lazycloud_api.services import fly_app_manager
from lazycloud_api.services.fly.schemas import FlyMachineConfig


@app.task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,  # 5 minutes max
    max_retries=3,
)
def create_machine_task(
    user_id: str, machine_name: str, machine_config_dict: Dict[str, Any]
):
    """Create a machine in the background"""
    # Convert dict back to FlyMachineConfig
    machine_config = FlyMachineConfig(**machine_config_dict)

    # Run the async function
    result = asyncio.run(
        fly_app_manager.create_machine(user_id, machine_name, machine_config)
    )

    logger.info(f"Successfully created machine {machine_name} for user {user_id}")
    return {"status": "success", "machine_id": result.id}


@app.task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,  # 2 minutes max
    max_retries=3,
)
def restart_machine_task(usage_uuid: str, machine_id: int):
    """Restart a machine in the background"""
    asyncio.run(fly_app_manager.restart_machine(usage_uuid, machine_id))

    logger.info(f"Successfully restarted machine {machine_id}")
    return {"status": "success", "machine_id": machine_id}


@app.task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,  # 5 minutes max
    max_retries=3,
)
def scale_machine_task(usage_uuid: str, machine_id: int, scale_config: Dict[str, Any]):
    """Scale a machine in the background"""
    # Validate required parameters
    cpu_kind = scale_config.get("cpu_kind")
    cpu = scale_config.get("cpu")
    memory = scale_config.get("memory")

    if cpu_kind is None or cpu is None or memory is None:
        raise ValueError("cpu_kind, cpu, and memory are required for scaling")

    asyncio.run(
        fly_app_manager.scale_machine(
            usage_uuid=usage_uuid,
            machine_id=machine_id,
            cpu_kind=cpu_kind,
            cpu=cpu,
            memory=memory,
        )
    )

    logger.info(f"Successfully scaled machine {machine_id}")
    return {"status": "success", "machine_id": machine_id}


@app.task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,  # 5 minutes max
    max_retries=3,
)
def delete_machine_task(usage_uuid: str, machine_id: int):
    """Delete a machine in the background"""
    # destroying the app will delete all machine associated entities
    result = asyncio.run(fly_app_manager.destroy_app(usage_uuid, machine_id))

    logger.info(f"Successfully deleted machine {machine_id}")
    return {"status": "success", "machine_id": machine_id, "result": result}


@app.task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,  # 2 minutes max
    max_retries=3,
)
def enable_machine_auto_stop_task(usage_uuid: str, machine_id: int, enabled: bool):
    """Enable/disable machine auto-stop in the background"""
    asyncio.run(
        fly_app_manager.auto_stop(
            usage_uuid=usage_uuid, machine_id=machine_id, enabled=enabled
        )
    )

    logger.info(
        f"Successfully {'enabled' if enabled else 'disabled'} auto-stop for machine {machine_id}"
    )
    return {"status": "success", "machine_id": machine_id, "auto_stop_enabled": enabled}
