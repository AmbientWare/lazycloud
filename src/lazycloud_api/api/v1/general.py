from importlib.metadata import version

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from lazycloud_api.config import app_config
from lazycloud_api.database.session import session_manager
from shared.responses.versions import CLIVersionResponse

health_router = APIRouter(prefix="/health", tags=["health"])
cli_version_router = APIRouter(prefix="/cli-version", tags=["cli-version"])


class HealthCheckResult(BaseModel):
    """Individual health check result."""

    status: str
    message: str | None = None


class PoolStats(BaseModel):
    """Database connection pool statistics."""

    pool_size: int
    checked_in: int
    checked_out: int
    overflow: int


class ReadinessResponse(BaseModel):
    """Response for readiness check."""

    status: str
    checks: dict[str, HealthCheckResult]
    pool_stats: PoolStats | None = None


@health_router.get("")
async def health() -> dict:
    """Liveness check - simple check that the service is running."""
    return {"status": "ok"}


@health_router.get("/ready", response_model=ReadinessResponse)
async def readiness() -> ReadinessResponse:
    """Readiness check - verifies all dependencies are accessible."""
    checks: dict[str, HealthCheckResult] = {}
    pool_stats: PoolStats | None = None

    # Database check
    try:
        async with session_manager.get_session() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = HealthCheckResult(status="ok")

        # Get pool stats (only for non-worker mode which uses connection pooling)
        if not app_config.IS_WORKER:
            pool = session_manager.engine.pool
            pool_stats = PoolStats(
                pool_size=pool.size(),
                checked_in=pool.checkedin(),
                checked_out=pool.checkedout(),
                overflow=pool.overflow(),
            )
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        checks["database"] = HealthCheckResult(status="error", message=str(e)[:100])

    # Redis check
    try:
        redis_client = aioredis.from_url(
            app_config.REDIS_URL, decode_responses=True, socket_timeout=2.0
        )
        await redis_client.ping()
        await redis_client.aclose()
        checks["redis"] = HealthCheckResult(status="ok")
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        checks["redis"] = HealthCheckResult(status="error", message=str(e)[:100])

    # Prometheus check
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{app_config.PROMETHEUS_URL}/-/healthy")
            if response.status_code == 200:
                checks["prometheus"] = HealthCheckResult(status="ok")
            else:
                checks["prometheus"] = HealthCheckResult(
                    status="error", message=f"HTTP {response.status_code}"
                )
    except Exception as e:
        logger.error(f"Prometheus health check failed: {e}")
        checks["prometheus"] = HealthCheckResult(status="error", message=str(e)[:100])

    # Determine overall status
    all_ok = all(check.status == "ok" for check in checks.values())
    overall_status = "ready" if all_ok else "degraded"

    return ReadinessResponse(
        status=overall_status,
        checks=checks,
        pool_stats=pool_stats,
    )


@cli_version_router.get("")
async def cli_version() -> CLIVersionResponse:
    """Get the minimum supported CLI version."""
    try:
        pkg_version = version("lazycloud")
    except Exception:
        pkg_version = "0.0.1"
    return CLIVersionResponse(version=pkg_version)
