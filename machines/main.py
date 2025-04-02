import dotenv

# we load the environment variables from the .env file first so we can use them in the app
dotenv.load_dotenv()

import argparse
import uvicorn
from fastapi import FastAPI, APIRouter
from contextlib import asynccontextmanager

# import app routers
from machines.api.v1.machines import machines_router
from machines.api.v1.health import health_router
from machines.api.middleware import setup_middleware
from machines.database.crud import create_tables

from machines.config import app_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events"""
    await create_tables()
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

# Configure Swagger UI
app.swagger_ui_init_oauth = {
    "useBasicAuthenticationWithAccessCodeGrant": False,
    "usePkceWithAuthorizationCodeGrant": False,
}

# Setup middleware
setup_middleware(app)

# create the main routes that will have the api version prefix
versionsed_routes = APIRouter(prefix=app_config.API_VERSION)
versionsed_routes.include_router(machines_router)
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
