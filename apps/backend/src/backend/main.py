import argparse
from contextlib import asynccontextmanager
from uuid import uuid4

import sentry_sdk
import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.loguru import LoguruIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from backend.api.v1 import (
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
from backend.config import ENVIRONMENT, app_config
from backend.lifecycle import shutdown_application, startup_application
from backend.log_config import setup_logger

# Setup logging
setup_logger()

# Initialize Sentry for error tracking and performance monitoring
if app_config.SENTRY_DSN:
    sentry_sdk.init(
        dsn=app_config.SENTRY_DSN,
        environment=app_config.ENV.value,
        traces_sample_rate=0.1,
        integrations=[
            FastApiIntegration(),
            SqlalchemyIntegration(),
            LoguruIntegration(),
        ],
    )
    logger.info("Sentry initialized for error tracking")


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
    # Dev mode warning
    if app_config.ENV == ENVIRONMENT.DEV:
        logger.warning(
            "RUNNING IN DEV MODE - Authentication bypass enabled! "
            "Set ENV=prod in production."
        )

    # Startup all application resources
    await startup_application()

    yield

    # Shutdown all application resources
    await shutdown_application()


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
