"""SAQ queue configuration and Redis connection management."""

from saq import Queue

from backend.config import app_config

# Global queue instances
_background_queue: Queue | None = None
_cron_queue: Queue | None = None


def get_background_queue() -> Queue:
    """Get or create the background tasks queue.

    This queue handles API-triggered tasks like deploy, destroy, rollback, etc.
    """
    global _background_queue
    if _background_queue is None:
        _background_queue = Queue.from_url(
            app_config.REDIS_URL,
            name="lazycloud-background",
        )
    return _background_queue


def get_cron_queue() -> Queue:
    """Get or create the cron tasks queue.

    This queue handles scheduled tasks like usage collection, billing, cleanup, etc.
    """
    global _cron_queue
    if _cron_queue is None:
        _cron_queue = Queue.from_url(
            app_config.REDIS_URL,
            name="lazycloud-crons",
        )
    return _cron_queue


async def disconnect_queues() -> None:
    """Disconnect all queue connections.

    Should be called during worker shutdown.
    """
    global _background_queue, _cron_queue
    if _background_queue:
        await _background_queue.disconnect()
        _background_queue = None
    if _cron_queue:
        await _cron_queue.disconnect()
        _cron_queue = None
