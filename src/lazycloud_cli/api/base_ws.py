import asyncio
import json
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

import websockets

from lazycloud_cli.config import config


class BaseWsAPI:
    """Base class for WebSocket API endpoints."""

    def __init__(self, url_path: str = ""):
        self._base_path = url_path
        self._websocket: Any | None = None
        self._running = False
        self._retry_count = 0
        self._max_retries = 3
        self._retry_delay = 5

    def _get_ws_url(self, path: str) -> str:
        """Get the WebSocket URL for the given path."""
        parsed = urlparse(config.api_base_url)

        # Change scheme to ws/wss
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"

        # Build the WebSocket URL
        ws_parsed = parsed._replace(scheme=ws_scheme)
        base_url = urlunparse(ws_parsed)

        # Add API version and path
        full_path = f"{config.api_version}/ws{path}"
        return f"{base_url}/{full_path}"

    def _add_auth_to_url(self, url: str) -> str:
        """Add authentication token to WebSocket URL as query parameter."""
        if config.active_api_key:
            api_key = config.active_api_key_value
            if api_key:
                separator = "&" if "?" in url else "?"
                return f"{url}{separator}token={api_key}"
        return url

    async def connect_with_retry(
        self,
        url_path: str,
        on_connect: Callable | None = None,
        on_message: Callable[[dict[str, Any]], None] = None,
        on_error: Callable[[Exception], None] | None = None,
        message_type: str = "log",
    ) -> None:
        """Connect to WebSocket with retry logic."""
        url = self._get_ws_url(url_path)
        url = self._add_auth_to_url(url)

        while self._retry_count < self._max_retries:
            try:
                await self._connect(url, on_connect, on_message, on_error, message_type)
                break
            except Exception as e:
                self._retry_count += 1
                if self._retry_count >= self._max_retries:
                    if on_error:
                        on_error(
                            Exception(f"Failed after {self._max_retries} retries: {e}")
                        )
                    raise
                else:
                    if on_error:
                        on_error(
                            Exception(
                                f"Connection failed, retrying in {self._retry_delay}s... ({self._retry_count}/{self._max_retries})"
                            )
                        )
                    await asyncio.sleep(self._retry_delay)

    async def _connect(
        self,
        url: str,
        on_connect: Callable | None = None,
        on_message: Callable[[dict[str, Any]], None] = None,
        on_error: Callable[[Exception], None] | None = None,
        message_type: str = "log",
    ) -> None:
        """Core WebSocket connection logic."""
        try:
            self._running = True

            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=10,
            ) as websocket:
                self._websocket = websocket
                self._retry_count = 0

                if on_connect:
                    await on_connect(websocket)

                # Start receiving messages
                while self._running:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=60.0)
                        data = json.loads(message)

                        if data.get("type") == message_type:
                            on_message(data.get("data", {}))
                        elif (
                            data.get("type") == "status_update"
                            and message_type == "status_update"
                        ):
                            on_message(data.get("data", {}))
                        elif data.get("type") == "error":
                            if on_error:
                                on_error(Exception(data.get("error", "Unknown error")))
                        elif data.get("type") == "pong":
                            pass  # Ignore pong responses

                    except asyncio.TimeoutError:
                        await websocket.send(json.dumps({"type": "ping"}))

                    except websockets.exceptions.ConnectionClosed:
                        break

                    except Exception as e:
                        if on_error:
                            on_error(e)
                        break

        except Exception as e:
            if on_error:
                on_error(e)
            raise

        finally:
            self._websocket = None
            self._running = False

    async def disconnect(self):
        """Disconnect the WebSocket."""
        self._running = False
        if self._websocket:
            await self._websocket.close()

    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._websocket is not None and self._running
