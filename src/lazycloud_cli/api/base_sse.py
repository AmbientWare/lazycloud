import asyncio
import json
from typing import Any, Callable

import httpx

from lazycloud_cli.config import config


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
        """Core SSE streaming implementation."""
        response = None
        try:
            self._running = True

            # Setup headers
            headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
            if config.active_api_key:
                api_key = config.active_api_key_value
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"

            # Create streaming connection
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))
            request = self._client.build_request("GET", url, headers=headers)
            response = await self._client.send(request, stream=True)

            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}"
                try:
                    error_data = await response.aread()
                    error_msg = f"{error_msg}: {error_data.decode()}"
                except Exception:
                    pass
                raise Exception(error_msg)

            # Parse SSE stream
            current_event = None
            current_data = []

            async for line in response.aiter_lines():
                if not self._running:
                    break

                line = line.strip()

                # Empty line = end of event
                if not line:
                    if current_data:
                        data_str = "\n".join(current_data)
                        try:
                            data = json.loads(data_str)
                            if current_event == "error":
                                if on_error:
                                    on_error(
                                        Exception(data.get("message", "Unknown error"))
                                    )
                            else:
                                on_event(current_event or "message", data)
                        except json.JSONDecodeError as e:
                            if on_error:
                                on_error(Exception(f"Invalid JSON in SSE data: {e}"))

                    current_event = None
                    current_data = []
                    continue

                # Parse SSE fields
                if line.startswith(":"):
                    continue  # Comment/keepalive
                elif line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    current_data.append(line[5:].strip())

        except Exception as e:
            if on_error:
                on_error(e)
            raise

        finally:
            self._running = False
            if response:
                await response.aclose()

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
