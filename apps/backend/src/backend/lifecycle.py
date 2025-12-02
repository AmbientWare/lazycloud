"""Application lifecycle management for startup and shutdown."""

from loguru import logger

from backend.database.crud import update_admin_api_keys
from backend.database.session import shutdown_database
from backend.services import get_depot_service
from backend.services.k8s.client import close_async_api_client
from backend.services.monitoring import (
    initialize_subscription_manager,
    shutdown_subscription_manager,
)


async def startup_application() -> None:
    """Initialize all application resources."""
    logger.info("Starting up application")

    # Update admin API keys (creates admin user/workspace if needed)
    await update_admin_api_keys()

    # Initialize subscription manager for shared monitoring (sse streams)
    initialize_subscription_manager()
    logger.info("Subscription manager initialized")

    logger.info("Application startup complete")


async def shutdown_application():
    """Shutdown all application resources."""
    logger.info("Shutting down application")

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
