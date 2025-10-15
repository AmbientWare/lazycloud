import asyncio
import json
from typing import Callable

from loguru import logger

from lazycloud_api.services.monitoring import (
    DeploymentMonitor,
    LogMonitor,
    ServiceMonitor,
    TaskMonitor,
)


async def create_sse_stream(
    monitor: DeploymentMonitor | ServiceMonitor | LogMonitor | TaskMonitor,
    event_type: str,
    format_data: Callable,
    stream_id: str,
):
    """Generic SSE stream generator for monitors.

    Yields dict objects for sse-starlette EventSourceResponse.
    Dict keys: "event", "data", "id", "retry", "comment"
    """
    try:
        queue: asyncio.Queue = asyncio.Queue()
        # Access private attribute since there's no property setter
        monitor._callback = lambda data: queue.put_nowait(data)

        await monitor.start()
        logger.info(f"SSE connected: {stream_id}")

        while monitor._running:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=30.0)
                # sse-starlette requires data to be JSON-encoded string
                yield {"event": event_type, "data": json.dumps(format_data(data))}

                # Check if monitor stopped after processing data
                if not monitor._running:
                    logger.info(f"SSE monitor stopped, closing stream: {stream_id}")
                    break

            except asyncio.TimeoutError:
                yield {"comment": "keepalive"}
                # Check if monitor is still running after timeout
                if not monitor._running:
                    logger.info(f"SSE monitor stopped during keepalive: {stream_id}")
                    break
            except Exception as e:
                logger.error(
                    f"SSE data processing error for {stream_id}: {e}", exc_info=True
                )
                # sse-starlette requires data to be JSON-encoded string
                yield {"event": "error", "data": json.dumps({"message": str(e)})}

    except asyncio.CancelledError:
        logger.info(f"SSE cancelled: {stream_id}")
        raise
    except Exception as e:
        logger.error(f"SSE fatal error for {stream_id}: {e}", exc_info=True)
        yield {"event": "error", "data": json.dumps({"message": str(e)})}
    finally:
        await monitor.stop()
        logger.info(f"SSE disconnected: {stream_id}")
