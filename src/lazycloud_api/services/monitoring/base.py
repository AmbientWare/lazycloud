import asyncio
from abc import ABC, abstractmethod
from typing import Callable, Generic, TypeVar

from loguru import logger

T = TypeVar("T")


POLL_INTERVAL = 2  # seconds


class BaseMonitor(ABC, Generic[T]):
    """Watches Kubernetes resources and provides real-time status updates."""

    def __init__(
        self, name: str, detail: str, callback: Callable[[T], None] | None = None
    ):
        self._name = name
        self._detail = detail
        self._callback = callback

        # State
        self._latest_result: T | None = None
        self._running = False
        self._watch_task: asyncio.Task | None = None

    @abstractmethod
    async def _task(self):
        """Abstract method to be implemented by subclasses"""
        pass

    async def start(self):
        """Start monitoring deployment status"""
        if self._running:
            return

        self._running = True
        self._watch_task = asyncio.create_task(self._watch())
        logger.info(f"Started monitoring {self._name} {self._detail}")

    async def stop(self):
        """Stop monitoring deployment status"""
        self._running = False
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
        logger.info(f"Stopped monitoring {self._name} {self._detail}")

    async def _watch(self):
        """Main watch loop that monitors for changes"""
        while self._running:
            try:
                task_result = await self._task()

                if task_result != self._latest_result:
                    self._latest_result = task_result
                    if self._callback:
                        if asyncio.iscoroutinefunction(self._callback):
                            await self._callback(task_result)
                        else:
                            self._callback(task_result)

                await asyncio.sleep(POLL_INTERVAL)

            except asyncio.CancelledError:
                break

            except Exception as e:
                logger.error(f"Error in watch loop: {e}")
                await asyncio.sleep(POLL_INTERVAL)


class BaseGenerativeMonitor(ABC, Generic[T]):
    """Base class for monitors that generate continuous streams of data"""

    def __init__(
        self, name: str, detail: str, callback: Callable[[T], None] | None = None
    ):
        self._name = name
        self._detail = detail
        self._callback = callback
        self._running = False
        self._stream_task: asyncio.Task | None = None

    @abstractmethod
    async def _stream(self):
        """
        Stream data continuously.

        This method should:
        1. Generate/stream data continuously while self._running is True
        2. Call self._emit(data) for each piece of data
        3. Handle its own error recovery
        4. Exit when self._running becomes False
        """
        pass

    async def _emit(self, data: T):
        """Emit data to the callback."""
        if self._callback:
            if asyncio.iscoroutinefunction(self._callback):
                await self._callback(data)
            else:
                self._callback(data)

    async def start(self):
        """Start the generative monitor."""
        if self._running:
            return

        self._running = True
        self._stream_task = asyncio.create_task(self._run_stream())
        logger.info(f"Started {self._name} for {self._detail}")

    async def stop(self):
        """Stop the generative monitor."""
        self._running = False

        # Allow subclasses to perform cleanup
        await self._cleanup()

        # Cancel the stream task
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass

        logger.info(f"Stopped {self._name} for {self._detail}")

    async def _cleanup(self):
        """Override to perform cleanup (e.g., stopping subprocesses, etc.)"""
        pass

    async def _run_stream(self):
        """Run the stream with error handling"""
        try:
            await self._stream()
        except asyncio.CancelledError:
            logger.debug(f"{self._name} stream task cancelled")
        except Exception as e:
            logger.error(f"Error in {self._name} stream: {e}")
            # Emit error to callback if still running
            if self._running and self._callback:
                try:
                    await self._emit(f"ERROR: Stream failed: {str(e)}")  # type: ignore
                except Exception:
                    pass  # Ignore callback errors
