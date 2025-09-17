from fastapi import APIRouter

from shared.responses.versions import CLIVersionResponse

health_router = APIRouter(prefix="/health", tags=["health"])
cli_version_router = APIRouter(prefix="/cli-version", tags=["cli-version"])


@health_router.get("")
async def health() -> dict:
    return {"status": "ok"}


@cli_version_router.get("")
async def cli_version() -> CLIVersionResponse:
    # TODO: get the version dynamically
    return CLIVersionResponse(version="0.0.1")
