import argparse
import multiprocessing as mp
from contextlib import asynccontextmanager
from uuid import uuid4

import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from lazycloud_api.api.v1 import (
    api_keys_router,
    cli_version_router,
    deployments_router,
    diff_router,
    health_router,
    invitations_router,
    tasks_router,
    users_router,
    workspaces_router,
)
from lazycloud_api.config import ENVIRONMENT, app_config
from lazycloud_api.database.crud import update_admin_api_keys
from lazycloud_api.log_config import setup_logger
from lazycloud_api.prefect_app import serve_background_tasks
from lazycloud_api.services import get_depot_service
from lazycloud_api.services.k8s.client import close_async_api_client
from lazycloud_api.services.monitoring import (
    initialize_subscription_manager,
    shutdown_subscription_manager,
)

# Setup logging
setup_logger()


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Middleware to add request ID to all requests for distributed tracing."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid4()))
        with logger.contextualize(request_id=request_id):
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events"""
    logger.info("Starting up application")

    # Dev mode warning
    if app_config.ENV == ENVIRONMENT.DEV:
        logger.warning(
            "RUNNING IN DEV MODE - Authentication bypass enabled! "
            "Set ENV=prod in production."
        )

    await update_admin_api_keys()

    # Initialize subscription manager for shared monitoring (sse streams)
    initialize_subscription_manager()
    logger.info("Subscription manager initialized")

    # start the prefect background tasks in a separate process
    ctx = mp.get_context("spawn")
    prefect_worker_process = ctx.Process(target=serve_background_tasks, daemon=True)
    prefect_worker_process.start()
    app.state.prefect_worker_process = prefect_worker_process
    logger.info(f"Prefect worker process started with PID {prefect_worker_process.pid}")

    logger.info("Application startup complete")
    yield
    logger.info("Shutting down application")

    # Shutdown Prefect worker process
    if hasattr(app.state, "prefect_worker_process"):
        worker_process = app.state.prefect_worker_process
        if worker_process.is_alive():
            logger.info(
                f"Terminating Prefect worker process (PID {worker_process.pid})"
            )
            worker_process.terminate()
            worker_process.join(timeout=5)
            if worker_process.is_alive():
                logger.warning(
                    "Prefect worker process did not terminate gracefully, forcing shutdown"
                )
                worker_process.kill()
                worker_process.join(timeout=2)
            logger.info("Prefect worker process terminated")
        else:
            logger.warning(
                f"Prefect worker process (PID {worker_process.pid}) was not alive"
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

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=app_config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add request ID middleware for distributed tracing
app.add_middleware(RequestIDMiddleware)

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
versionsed_routes.include_router(workspaces_router)
versionsed_routes.include_router(invitations_router)
versionsed_routes.include_router(diff_router)
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
