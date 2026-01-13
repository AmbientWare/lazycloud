"""Cron worker for SAQ scheduled task execution.

This module serves the SAQ cron workers that handle scheduled tasks
like usage collection, billing, cleanup, etc.
"""

import asyncio

import sentry_sdk
from loguru import logger
from saq import Worker
from sentry_sdk.integrations.loguru import LoguruIntegration

from backend.config import app_config
from backend.tasks.queue import get_cron_queue, disconnect_queues
from backend.tasks.crons import CRON_FUNCTIONS, CRON_JOBS

# Initialize Sentry
if app_config.SENTRY_DSN:
    sentry_sdk.init(
        dsn=app_config.SENTRY_DSN,
        environment=app_config.ENV.value,
        integrations=[LoguruIntegration()],
    )
    logger.info("Sentry initialized for SAQ cron worker")


async def startup(ctx: dict):
    """Worker startup hook."""
    logger.info("SAQ cron worker starting up")


async def shutdown(ctx: dict):
    """Worker shutdown hook."""
    logger.info("SAQ cron worker shutting down")
    await disconnect_queues()


async def run_worker():
    """Run the SAQ cron worker."""
    queue = get_cron_queue()

    worker = Worker(
        queue=queue,
        functions=CRON_FUNCTIONS,
        cron_jobs=CRON_JOBS,
        concurrency=5,  # Lower concurrency for cron jobs
        startup=startup,
        shutdown=shutdown,
    )

    logger.info(f"Starting SAQ cron worker with {len(CRON_JOBS)} scheduled jobs")
    await worker.start()


def main():
    """Start the SAQ cron worker."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
