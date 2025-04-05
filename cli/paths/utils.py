import time
import sys
import os
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable
from tabulate import tabulate
import threading
from dataclasses import dataclass
from enum import Enum
from cli.config import config
from cli.logging import logger
import click


class SpinnerStyle(Enum):
    """Spinner animation styles"""

    DOTS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    ARROWS = "←↖↑↗→↘↓↙"
    SIMPLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass
class SpinnerConfig:
    """Configuration for spinner behavior"""

    update_interval: float = 0.05
    style: SpinnerStyle = SpinnerStyle.DOTS
    clear_on_exit: bool = True
    color: str = "white"  # Color for the spinner message


class Spinner:
    """
    A thread-safe spinner for displaying loading states in the terminal.
    """

    def __init__(
        self, message: str = "Processing", config: Optional[SpinnerConfig] = None
    ):
        self.message = message
        self.config = config or SpinnerConfig()
        self.spinner_chars = self.config.style.value
        self.spinner_index = 0
        self.running = False
        self._last_update = 0
        self._output_lock = threading.Lock()
        self._thread = threading.Thread(target=self._spin_thread)
        self._thread.daemon = True

    def __enter__(self) -> "Spinner":
        """Context manager entry"""
        self.running = True
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[Exception],
        exc_tb: Optional[Any],
    ) -> None:
        """Context manager exit"""
        self.running = False
        if self.config.clear_on_exit:
            self._clear_line()

    def _clear_line(self) -> None:
        """Clear the current line in the terminal"""
        with self._output_lock:
            sys.stdout.write("\r" + " " * (len(self.message) + 10) + "\r")
            sys.stdout.flush()

    def _spin_thread(self) -> None:
        """Background thread for spinner animation"""
        while self.running:
            self.update()
            time.sleep(self.config.update_interval)

    def update(self) -> None:
        """Update the spinner animation"""
        with self._output_lock:
            # Use click.style for colored output while maintaining the spinner animation
            styled_message = click.style(
                f"{self.spinner_chars[self.spinner_index]} {self.message}",
                fg=self.config.color,
            )
            sys.stdout.write(f"\r{styled_message}")
            sys.stdout.flush()
            self.spinner_index = (self.spinner_index + 1) % len(self.spinner_chars)


class StatusSpinner(Spinner):
    """
    A spinner that displays both animation and status updates.
    """

    def __init__(
        self,
        message: str,
        status_checker: Callable[[], str],
        status_interval: float = 5.0,
        config: Optional[SpinnerConfig] = None,
    ):
        super().__init__(message, config)
        self.status_checker = status_checker
        self.status_interval = status_interval
        self._last_status_check = 0
        self._last_status: Optional[str] = None
        self._base_message = message
        self._status_msg = ""

    def status_update(self, new_status_msg: str) -> None:
        """Update the status message"""
        self._status_msg = new_status_msg
        self.message = f"{self._base_message} - {self._status_msg}"

    def update(self) -> None:
        """Update both spinner animation and status"""
        current_time = time.time()

        if current_time - self._last_status_check >= self.status_interval:
            try:
                status = self.status_checker()
                if status != self._last_status:
                    self.status_update(f"Status: {status}")
                    self._last_status = status

            except Exception as e:
                logger.error(f"Error checking status: {e}")
                self.status_update("Error checking status")

            self._last_status_check = current_time

        super().update()


def get_config_path() -> Path:
    """Get the path to the .machines config file"""
    return Path.home() / ".machines"


def get_active_api_key() -> Optional[str]:
    """Get the currently active API key"""
    return config.active_api_key


def get_default_key_path() -> str:
    """Get the system's default SSH key path"""
    return config.default_ssh_key_path


def get_default_ssh_config_path() -> str:
    """Get the system's default SSH config path"""
    return config.ssh_config_path


def make_table_view(
    data: List[Dict[str, Any]],
    headers: List[str],
    tablefmt: str = "grid",
    exclude_fields: Optional[List[str]] = None,
) -> str:
    """
    Create a formatted table view from data.

    Args:
        data: List of dictionaries containing the data
        headers: List of header names
        tablefmt: Format string for tabulate
        exclude_fields: Fields to exclude from the output

    Returns:
        Formatted table string
    """
    exclude_fields = exclude_fields or ["id", "user_id", "created_at", "updated_at"]

    filtered_data = [
        {k: v for k, v in row.items() if k not in exclude_fields} for row in data
    ]

    filtered_headers = [h for h in headers if h not in exclude_fields]
    table_data = [
        [row.get(header, "") for header in filtered_headers] for row in filtered_data
    ]

    return tabulate(table_data, headers=filtered_headers, tablefmt=tablefmt)
