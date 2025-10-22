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
        self._callbacks: list[Callable[[T], None]] = []
        if callback:
            self._callbacks.append(callback)

        # State
        self._latest_result: T | None = None
        self._running = False
        self._watch_task: asyncio.Task | None = None
        self._callback_lock = asyncio.Lock()

    @abstractmethod
    async def _task(self):
        """Abstract method to be implemented by subclasses"""
        pass

    async def add_callback(self, callback: Callable[[T], None]) -> None:
        """Add a callback to be notified of status updates."""
        async with self._callback_lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)
                logger.debug(
                    f"Added callback to {self._name} {self._detail}. "
                    f"Total subscribers: {len(self._callbacks)}"
                )

    async def remove_callback(self, callback: Callable[[T], None]) -> None:
        """Remove a callback from notifications."""
        async with self._callback_lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)
                logger.debug(
                    f"Removed callback from {self._name} {self._detail}. "
                    f"Remaining subscribers: {len(self._callbacks)}"
                )

    def has_subscribers(self) -> bool:
        """Check if this monitor has any active subscribers."""
        return len(self._callbacks) > 0

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
                    await self._emit(task_result)

                await asyncio.sleep(POLL_INTERVAL)

            except asyncio.CancelledError:
                break

            except Exception as e:
                logger.error(f"Error in watch loop: {e}")
                await asyncio.sleep(POLL_INTERVAL)

    async def _emit(self, data: T):
        """Emit data to all registered callbacks."""
        async with self._callback_lock:
            callbacks = self._callbacks.copy()

        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
            except Exception as e:
                logger.error(f"Error in callback for {self._name} {self._detail}: {e}")


class BaseGenerativeMonitor(ABC, Generic[T]):
    """Base class for monitors that generate continuous streams of data"""

    def __init__(
        self, name: str, detail: str, callback: Callable[[T], None] | None = None
    ):
        self._name = name
        self._detail = detail
        self._callbacks: list[Callable[[T], None]] = []
        if callback:
            self._callbacks.append(callback)
        self._running = False
        self._stream_task: asyncio.Task | None = None
        self._callback_lock = asyncio.Lock()

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

    async def add_callback(self, callback: Callable[[T], None]) -> None:
        """Add a callback to be notified of status updates."""
        async with self._callback_lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)
                logger.debug(
                    f"Added callback to {self._name} {self._detail}. "
                    f"Total subscribers: {len(self._callbacks)}"
                )

    async def remove_callback(self, callback: Callable[[T], None]) -> None:
        """Remove a callback from notifications."""
        async with self._callback_lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)
                logger.debug(
                    f"Removed callback from {self._name} {self._detail}. "
                    f"Remaining subscribers: {len(self._callbacks)}"
                )

    def has_subscribers(self) -> bool:
        """Check if this monitor has any active subscribers."""
        return len(self._callbacks) > 0

    async def _emit(self, data: T):
        """Emit data to all registered callbacks."""
        async with self._callback_lock:
            callbacks = self._callbacks.copy()

        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
            except Exception as e:
                logger.error(f"Error in callback for {self._name} {self._detail}: {e}")

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
            # Emit error to callbacks if still running
            if self._running and self.has_subscribers():
                try:
                    await self._emit(f"ERROR: Stream failed: {str(e)}")  # type: ignore
                except Exception:
                    pass  # Ignore callback errors
