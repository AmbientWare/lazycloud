import dotenv

# we load the environment variables from the .env file first so we can use them in rest of the app
dotenv.load_dotenv()

import argparse
import uvicorn
from fastapi import FastAPI, APIRouter
from contextlib import asynccontextmanager
from slowapi.middleware import SlowAPIMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi import Limiter

# import app routers
from machines.api.v1.machines import machines_router
from machines.api.v1.health import health_router
from machines.api.v1.users import users_router
from machines.api.v1.api_keys import api_keys_router

# import other modules
from machines.database.crud import create_tables, update_admin_api_keys
from machines.config import app_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events"""
    await create_tables()
    await update_admin_api_keys()
    yield


# create a fastapi app
app = FastAPI(
    title="Machines API",
    description="API for managing machines",
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
versionsed_routes.include_router(machines_router)
versionsed_routes.include_router(users_router)
versionsed_routes.include_router(api_keys_router)
app.include_router(versionsed_routes)

# include non versioned routes that are not part of the main api
non_versionsed_routes = APIRouter()
non_versionsed_routes.include_router(health_router)
app.include_router(non_versionsed_routes)

# run the app with uvicorn
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--workers", type=int, default=1)

    args = parser.parse_args()

    uvicorn.run(
        "machines.main:app",
        host="0.0.0.0",
        port=8000,
        reload=args.debug,
        workers=args.workers,
    )
