import time
from typing import Any, Optional, Callable
import threading

from lazycloud_cli.logging import logger


def mb_to_gb(mb: int) -> int:
    """Convert MB to GB"""
    return mb // 1024


def gb_to_mb(gb: int) -> int:
    """Convert GB to MB"""
    return gb * 1024


class Spinner:
    """
    A simple context manager for displaying a spinner using Rich's Progress.
    """

    def __init__(self, message: str = "Processing..."):
        self.message = message
        self._progress = None
        self._task_id = None

    def __enter__(self) -> "Spinner":
        """Context manager entry"""
        self._progress, self._task_id = logger.create_progress_spinner(self.message)
        if self._progress:
            self._progress.start()
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[Exception],
        exc_tb: Optional[Any],
    ) -> None:
        """Context manager exit"""
        if self._progress:
            self._progress.stop()


class StatusSpinner:
    """
    A spinner that displays status updates alongside the animation using Rich's Progress.
    It uses a background thread to periodically call a status checking function.
    """

    def __init__(
        self,
        message: str,
        status_checker: Callable[[], str],
        status_interval: float = 5.0,
    ):
        self._base_message = message
        self.status_checker = status_checker
        self.status_interval = status_interval
        self._progress = None
        self._task_id = None
        self._last_status_check = 0
        self._last_status: Optional[str] = None
        self._status_msg = ""
        self._status_thread = None
        self._running = False

    def _update_description(self, status_msg: Optional[str] = None) -> None:
        """Update the spinner's description."""
        if self._progress and self._task_id is not None:
            description = self._base_message
            if status_msg:
                description += f"\n[yellow]Status: {status_msg}[/yellow]"
            try:
                self._progress.update(self._task_id, description=description)
            except Exception as e:
                # Log errors during update, but don't crash the thread
                logger.debug(f"Error updating spinner description: {e}")

    def _status_check_thread(self) -> None:
        """Background thread for checking status"""
        while self._running:
            current_time = time.time()
            if current_time - self._last_status_check >= self.status_interval:
                self._last_status_check = current_time
                try:
                    # Get the status
                    status = self.status_checker()

                    if status != self._last_status:
                        self._update_description(status)
                        self._last_status = status

                except Exception as e:
                    # Log the error but don't update the status to avoid potential conflicts
                    logger.warning(f"Error checking status: {e}")

                self._last_status_check = current_time
            time.sleep(0.1)  # Small sleep to prevent CPU hogging

    def __enter__(self) -> "StatusSpinner":
        """Context manager entry"""
        # Start with the base message
        self._progress, self._task_id = logger.create_progress_spinner(self._base_message)
        self._progress.start()
        self._running = True
        self._status_thread = threading.Thread(target=self._status_check_thread)
        self._status_thread.daemon = True
        self._status_thread.start()
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[Exception],
        exc_tb: Optional[Any],
    ) -> None:
        """Context manager exit"""
        self._running = False
        # Wait briefly for the thread to finish its current loop
        if self._status_thread:
            self._status_thread.join(timeout=1.0)
        # Ensure the spinner stops
        if self._progress:
            self._progress.stop()
