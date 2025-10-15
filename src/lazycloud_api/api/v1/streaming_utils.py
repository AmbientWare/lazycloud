"""Shared utilities for SSE/streaming endpoints."""

import asyncio
import json
from typing import Callable

from loguru import logger

from lazycloud_api.services.monitoring import (
    DeploymentMonitor,
    LogMonitor,
    ServiceMonitor,
)


def format_sse(event: str, data: dict) -> str:
    """Format data as Server-Sent Event."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def create_sse_stream(
    monitor: DeploymentMonitor | ServiceMonitor | LogMonitor,
    event_type: str,
    format_data: Callable,
    stream_id: str,
):
    """Generic SSE stream generator for monitors."""
    try:
        queue: asyncio.Queue = asyncio.Queue()
        # Access private attribute since there's no property setter
        monitor._callback = lambda data: queue.put_nowait(data)

        await monitor.start()
        logger.info(f"SSE connected: {stream_id}")

        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield format_sse(event_type, format_data(data))
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
            except Exception as e:
                logger.error(
                    f"SSE data processing error for {stream_id}: {e}", exc_info=True
                )
                yield format_sse("error", {"message": str(e)})

    except asyncio.CancelledError:
        logger.info(f"SSE cancelled: {stream_id}")
        raise
    except Exception as e:
        logger.error(f"SSE fatal error for {stream_id}: {e}", exc_info=True)
        yield format_sse("error", {"message": str(e)})
    finally:
        await monitor.stop()
        logger.info(f"SSE disconnected: {stream_id}")
