import asyncio
import json
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from loguru import logger
from models.monitoring import StreamEventType

from backend.services.monitoring import LogMonitor, get_subscription_manager
from backend.services.monitoring.monitor_config import (
    LogMonitorConfig,
    MonitorConfig,
)

# NOTE: maybe make this configurable?
STATUS_QUEUE_SIZE = 100
LOG_QUEUE_SIZE = 500


async def _sse_event_loop(
    queue: asyncio.Queue,
    event_type: StreamEventType,
    format_data: Callable,
    stream_id: str,
    should_continue: Callable[[], bool] | None = None,
):
    """Common SSE event streaming logic"""
    while should_continue is None or should_continue():
        try:
            data = await asyncio.wait_for(queue.get(), timeout=30.0)
            yield {"event": event_type, "data": json.dumps(format_data(data))}

            if should_continue and not should_continue():
                logger.info(f"Stream condition ended for {stream_id}")
                break

        except asyncio.TimeoutError:
            yield {"comment": "keepalive"}
            if should_continue and not should_continue():
                logger.info(f"Stream condition ended during keepalive for {stream_id}")
                break

        except Exception as e:
            logger.error(
                f"SSE data processing error for {stream_id}: {e}", exc_info=True
            )
            yield {"event": "error", "data": json.dumps({"message": str(e)})}


async def create_sse_stream_with_subscription(
    config: MonitorConfig,
    event_type: StreamEventType,
    format_data: Callable,
    stream_id: str,
):
    """Generic SSE stream generator using subscription manager"""
    subscription_manager = get_subscription_manager()
    monitor_key = None
    subscription_id = None
    # Use LOG_QUEUE_SIZE for log streams, STATUS_QUEUE_SIZE for status streams
    queue_size = (
        LOG_QUEUE_SIZE if isinstance(config, LogMonitorConfig) else STATUS_QUEUE_SIZE
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)

    def callback(data):
        """Callback to receive data from shared monitor."""
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning(f"Queue full for {stream_id}, dropping data")

    try:
        # Subscribe to monitor
        monitor_key, subscription_id = await subscription_manager.subscribe(
            config=config,
            callback=callback,
        )
        logger.info(f"SSE connected: {stream_id} (subscription: {subscription_id})")

        # Stream data using common event loop
        # Check if monitor is still running to handle terminal states (e.g., task completion)
        async for event in _sse_event_loop(
            queue,
            event_type,
            format_data,
            stream_id,
            should_continue=lambda: subscription_manager.is_monitor_running(
                monitor_key
            ),
        ):
            yield event

    except asyncio.CancelledError:
        logger.info(f"SSE cancelled: {stream_id}")
        raise

    except Exception as e:
        logger.error(f"SSE fatal error for {stream_id}: {e}", exc_info=True)
        yield {"event": "error", "data": json.dumps({"message": str(e)})}

    finally:
        # Unsubscribe from monitor
        if monitor_key is not None and subscription_id is not None:
            await subscription_manager.unsubscribe(monitor_key, subscription_id)
        logger.info(f"SSE disconnected: {stream_id}")


async def create_sse_stream_direct(
    monitor: LogMonitor,
    event_type: StreamEventType,
    format_data: Callable,
    stream_id: str,
):
    """Direct SSE stream generator for per-connection monitors"""
    queue: asyncio.Queue = asyncio.Queue(maxsize=LOG_QUEUE_SIZE)

    def callback(data):
        """Callback to receive data from monitor."""
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning(f"Queue full for {stream_id}, dropping data")

    try:
        await monitor.add_callback(callback)
        await monitor.start()
        logger.info(f"SSE connected: {stream_id}")

        # Stream data using common event loop with monitor running check
        async for event in _sse_event_loop(
            queue, event_type, format_data, stream_id, lambda: monitor._running
        ):
            yield event

    except asyncio.CancelledError:
        logger.info(f"SSE cancelled: {stream_id}")
        raise

    except Exception as e:
        logger.error(f"SSE fatal error for {stream_id}: {e}", exc_info=True)
        yield {"event": "error", "data": json.dumps({"message": str(e)})}

    finally:
        await monitor.remove_callback(callback)
        await monitor.stop()
        logger.info(f"SSE disconnected: {stream_id}")


def get_calendar_day_in_timezone(utc_datetime: datetime, tz: ZoneInfo) -> str:
    """Get the calendar day (YYYY-MM-DD) in the given timezone from a UTC datetime."""
    local_time = utc_datetime.astimezone(tz)
    return local_time.strftime("%Y-%m-%d")


def get_utc_midnight_for_calendar_day(calendar_day: str, tz: ZoneInfo) -> datetime:
    """Get UTC datetime for midnight of the calendar day in the given timezone."""
    local_midnight = datetime.strptime(calendar_day, "%Y-%m-%d").replace(
        tzinfo=tz, hour=0, minute=0, second=0, microsecond=0
    )
    return local_midnight.astimezone(timezone.utc)


def normalize_usage_date_range(
    start_date: datetime | None, end_date: datetime | None
) -> tuple[datetime, datetime]:
    """Normalize date range for usage queries (defaults to current month)."""
    now = datetime.now(timezone.utc)
    if not start_date:
        start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if not end_date:
        end_date = now
    return start_date, end_date
