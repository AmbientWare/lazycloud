import argparse
import multiprocessing as mp
from contextlib import asynccontextmanager

import uvicorn
from fastapi import APIRouter, FastAPI
from loguru import logger
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from lazycloud_api.api.v1.api_keys import api_keys_router
from lazycloud_api.api.v1.deployments import router as deployments_router
from lazycloud_api.api.v1.diff import diff_router
from lazycloud_api.api.v1.general import cli_version_router, health_router
from lazycloud_api.api.v1.instances import instances_router
from lazycloud_api.api.v1.registry import router as registry_router
from lazycloud_api.api.v1.secrets import secrets_router

# Services routes now under deployments module
from lazycloud_api.api.v1.tasks import tasks_router
from lazycloud_api.api.v1.usage import router as usage_router
from lazycloud_api.api.v1.users import users_router
from lazycloud_api.api.v1.workspaces import workspaces_router
from lazycloud_api.config import app_config
from lazycloud_api.database.crud import create_tables, update_admin_api_keys
from lazycloud_api.log_config import setup_logger
from lazycloud_api.prefect_app import serve_prefect_tasks
from lazycloud_api.services.monitoring import (
    initialize_subscription_manager,
    shutdown_subscription_manager,
)

# Setup logging
setup_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events"""
    logger.info("Starting up application")
    await create_tables()
    await update_admin_api_keys()

    # Initialize subscription manager for shared monitoring (sse streams)
    initialize_subscription_manager()
    logger.info("Subscription manager initialized")

    # start the prefect tasks in a separate process
    mp.get_context("spawn")
    mp.Process(target=serve_prefect_tasks).start()
    logger.info("Application startup complete")
    yield
    logger.info("Shutting down application")

    # Shutdown subscription manager
    await shutdown_subscription_manager()
    logger.info("Subscription manager shutdown complete")


# create a fastapi app
app = FastAPI(
    title="LazyCloud API",
    description="API for managing LazyCloud",
    version="1.0.0",
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Add rate limiting middleware, use redis to store the rate limit data
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[app_config.RATE_LIMIT],
    storage_uri=app_config.REDIS_URL,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore
app.add_middleware(SlowAPIMiddleware)

# Configure Swagger UI
app.swagger_ui_init_oauth = {
    "useBasicAuthenticationWithAccessCodeGrant": False,
    "usePkceWithAuthorizationCodeGrant": False,
}

# create the main routes that will have the api version prefix
versionsed_routes = APIRouter(prefix=app_config.API_VERSION)
versionsed_routes.include_router(users_router)
versionsed_routes.include_router(api_keys_router)
versionsed_routes.include_router(tasks_router)
versionsed_routes.include_router(deployments_router)
versionsed_routes.include_router(secrets_router)
versionsed_routes.include_router(instances_router)
versionsed_routes.include_router(diff_router)
versionsed_routes.include_router(registry_router)
versionsed_routes.include_router(workspaces_router)
versionsed_routes.include_router(usage_router)
app.include_router(versionsed_routes)

# include non versioned routes that are not part of the main api
non_versionsed_routes = APIRouter()
non_versionsed_routes.include_router(health_router)
non_versionsed_routes.include_router(cli_version_router)
app.include_router(non_versionsed_routes)


# run the app with uvicorn
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--workers", type=int, default=1)

    args = parser.parse_args()

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=args.debug,
        workers=args.workers,
    )
