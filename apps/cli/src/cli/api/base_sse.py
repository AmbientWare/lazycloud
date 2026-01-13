import asyncio
import json
from typing import Any, Callable

import httpx
from httpx_sse import aconnect_sse
from loguru import logger

from cli.config import config


class SSEClient:
    """Simple SSE (Server-Sent Events) client."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._running = False

    async def stream(
        self,
        path: str,
        on_event: Callable[[str, dict[str, Any]], None],
        on_error: Callable[[Exception], None] | None = None,
        max_retries: int = 3,
    ) -> None:
        """
        Connect to SSE endpoint and stream events.

        Args:
            path: Stream endpoint path (e.g., "/deployments/{id}/status/stream")
            on_event: Callback for events, receives (event_type, data)
            on_error: Optional error callback
            max_retries: Maximum number of connection retry attempts
        """
        url = f"{config.api_base_url}/{config.api_version}{path}"
        logger.debug(f"SSEClient.stream called for {url}")

        for attempt in range(max_retries):
            try:
                await self._stream(url, on_event, on_error)
                break  # Successful connection ended normally
            except Exception as e:
                if attempt + 1 >= max_retries:
                    if on_error:
                        on_error(Exception(f"Failed after {max_retries} retries: {e}"))
                    raise

                if on_error:
                    retry_in = 5
                    on_error(
                        Exception(
                            f"Connection failed, retrying in {retry_in}s... "
                            f"({attempt + 1}/{max_retries})"
                        )
                    )
                await asyncio.sleep(5)

    async def _stream(
        self,
        url: str,
        on_event: Callable[[str, dict[str, Any]], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Core SSE streaming implementation using httpx-sse."""
        logger.debug(f"SSEClient._stream connecting to {url}")
        try:
            self._running = True

            # Setup headers
            headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
            access_token = config.access_token
            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"

            # Create streaming connection
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))
            logger.debug(f"SSEClient._stream created client, opening SSE connection")

            # Use httpx-sse to handle SSE protocol
            async with aconnect_sse(
                self._client, "GET", url, headers=headers
            ) as event_source:
                logger.debug(f"SSEClient._stream SSE connection established")
                async for sse in event_source.aiter_sse():
                    if not self._running:
                        break

                    # Handle error events
                    if sse.event == "error":
                        logger.debug(f"SSEClient received error event: {sse.data}")
                        try:
                            data = json.loads(sse.data)
                            if on_error:
                                on_error(
                                    Exception(data.get("message", "Unknown error"))
                                )
                        except json.JSONDecodeError:
                            if on_error:
                                on_error(Exception("Invalid error data"))
                    else:
                        # Handle regular events
                        logger.debug(f"SSEClient received event: {sse.event}")
                        try:
                            data = json.loads(sse.data)
                            on_event(sse.event or "message", data)
                        except json.JSONDecodeError as e:
                            if on_error:
                                on_error(Exception(f"Invalid JSON in SSE data: {e}"))

        except Exception as e:
            logger.error(f"SSEClient._stream exception: {e}")
            if on_error:
                on_error(e)
            raise

        finally:
            self._running = False
            if self._client:
                await self._client.aclose()
                self._client = None

    async def disconnect(self):
        """Stop the SSE stream."""
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None

    def is_connected(self) -> bool:
        """Check if currently connected."""
        return self._running and self._client is not None
