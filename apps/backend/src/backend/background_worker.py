"""Background worker for SAQ task execution.

This module serves the SAQ task workers that handle background tasks
like deployments, deletions, restarts, etc. It runs as a separate service
independent of the API server.
"""

import asyncio

import sentry_sdk
from loguru import logger
from saq import Worker
from sentry_sdk.integrations.loguru import LoguruIntegration

from backend.config import app_config
from backend.services.k8s.client import close_all_clients, initialize_cluster_clients
from backend.tasks.jobs import BACKGROUND_JOBS
from backend.tasks.queue import disconnect_queues, get_background_queue

# Initialize Sentry
if app_config.SENTRY_DSN:
    sentry_sdk.init(
        dsn=app_config.SENTRY_DSN,
        environment=app_config.ENV.value,
        integrations=[LoguruIntegration()],
    )
    logger.info("Sentry initialized for SAQ background worker")


async def startup(ctx: dict):
    """Worker startup hook."""
    logger.info("SAQ background worker starting up")
    await initialize_cluster_clients()
    logger.info("Kubernetes cluster clients initialized")


async def shutdown(ctx: dict):
    """Worker shutdown hook."""
    logger.info("SAQ background worker shutting down")
    await close_all_clients()
    await disconnect_queues()


async def before_process(ctx: dict):
    """Called before each job processes."""
    job = ctx.get("job")
    if job:
        logger.debug(f"Processing job: {job.function} (key: {job.key})")


async def after_process(ctx: dict):
    """Called after each job processes."""
    job = ctx.get("job")
    if job:
        logger.debug(f"Finished job: {job.function} (status: {job.status})")


async def run_worker():
    """Run the SAQ background worker."""
    queue = get_background_queue()

    worker = Worker(
        queue=queue,
        functions=BACKGROUND_JOBS,
        concurrency=10,
        startup=startup,
        shutdown=shutdown,
        before_process=before_process,
        after_process=after_process,
    )

    logger.info(f"Starting SAQ background worker with {len(BACKGROUND_JOBS)} jobs")
    await worker.start()


def main():
    """Start the SAQ background worker."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
