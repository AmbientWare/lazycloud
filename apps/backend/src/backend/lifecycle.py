"""Application lifecycle management for startup and shutdown."""

import multiprocessing as mp
from typing import Any

from loguru import logger

from backend.database.crud import update_admin_api_keys
from backend.database.session import shutdown_database
from backend.prefect_app import serve_background_tasks
from backend.services import get_depot_service
from backend.services.k8s.client import close_async_api_client
from backend.services.monitoring import (
    initialize_subscription_manager,
    shutdown_subscription_manager,
)


async def startup_application() -> dict[str, Any]:
    """Initialize all application resources."""
    logger.info("Starting up application")

    # Update admin API keys (creates admin user/workspace if needed)
    await update_admin_api_keys()

    # Initialize subscription manager for shared monitoring (sse streams)
    initialize_subscription_manager()
    logger.info("Subscription manager initialized")

    # Start Prefect worker process for background tasks
    ctx = mp.get_context("spawn")
    prefect_worker_process = ctx.Process(target=serve_background_tasks, daemon=True)
    prefect_worker_process.start()
    logger.info(f"Prefect worker process started with PID {prefect_worker_process.pid}")

    logger.info("Application startup complete")

    return {"prefect_worker_process": prefect_worker_process}


async def shutdown_application(prefect_worker_process: mp.Process | None = None):
    """Shutdown all application resources."""
    logger.info("Shutting down application")

    # Shutdown Prefect worker process
    if prefect_worker_process is not None:
        if prefect_worker_process.is_alive():
            logger.info(
                f"Terminating Prefect worker process (PID {prefect_worker_process.pid})"
            )
            prefect_worker_process.terminate()
            prefect_worker_process.join(timeout=5)
            if prefect_worker_process.is_alive():
                logger.warning(
                    "Prefect worker process did not terminate gracefully, forcing shutdown"
                )
                prefect_worker_process.kill()
                prefect_worker_process.join(timeout=2)
            logger.info("Prefect worker process terminated")
        else:
            logger.warning(
                f"Prefect worker process (PID {prefect_worker_process.pid}) was not alive"
            )

    # Shutdown subscription manager
    await shutdown_subscription_manager()
    logger.info("Subscription manager shutdown complete")

    # Close async Kubernetes client
    await close_async_api_client()
    logger.info("Async Kubernetes client closed")

    # Close Depot service connections (Redis, HTTP client)
    try:
        depot_service = get_depot_service()
        await depot_service.close()
        logger.info("Depot service connections closed")
    except Exception as e:
        logger.warning(f"Error closing Depot service: {e}")

    # Close database connections
    await shutdown_database()

    logger.info("Application shutdown complete")
