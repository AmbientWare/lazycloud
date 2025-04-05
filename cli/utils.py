import time
import sys
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from tabulate import tabulate

from cli.config import config


class Spinner:
    def __init__(self, message: str = "Processing"):
        self.message = message
        self.spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self.spinner_index = 0
        self.running = False
        self._last_update = 0
        self._update_interval = 0.1

    def __enter__(self):
        self.running = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.running = False
        self._clear_line()

    def _clear_line(self):
        """Clear the current line"""
        sys.stdout.write("\r" + " " * (len(self.message) + 10) + "\r")
        sys.stdout.flush()

    def set_message(self, new_message: str):
        """Set a new message for the spinner"""
        self.message = new_message
        # Force an immediate update with the new message
        sys.stdout.write(f"\r{self.spinner_chars[self.spinner_index]} {self.message}")
        sys.stdout.flush()

    def update(self):
        """Update the spinner animation"""
        current_time = time.time()
        if current_time - self._last_update < self._update_interval:
            return

        sys.stdout.write(f"\r{self.spinner_chars[self.spinner_index]} {self.message}")
        sys.stdout.flush()
        self.spinner_index = (self.spinner_index + 1) % len(self.spinner_chars)
        self._last_update = current_time


class StatusSpinner(Spinner):
    def __init__(self, message: str, status_checker, status_interval: float = 5.0):
        super().__init__(message)
        self.status_checker = status_checker
        self.status_interval = status_interval
        self._last_status_check = 0
        self._last_status = None
        self._base_message = message  # Store the original message
        self._status_msg = ""

    def status_update(self, new_status_msg: str):
        """Set a new message for the spinner"""
        self._status_msg = new_status_msg
        super().set_message(f"{self._base_message} - {self._status_msg}")

    def update(self):
        """Update the spinner and check status if needed"""
        current_time = time.time()

        # Check status if interval has passed
        if current_time - self._last_status_check >= self.status_interval:
            try:
                status = self.status_checker()
                if status != self._last_status:
                    status_msg = status.get("status", "unknown")
                    # Update the message with the current status
                    self.status_update(f"Status: {status_msg}")
                    self._last_status = status

                    # Stop if machine is ready or failed
                    if status_msg in ["ready", "failed"]:
                        self.running = False
                        return

            except Exception:
                pass
            self._last_status_check = current_time

        # Update spinner animation
        super().update()


def get_config_path() -> Path:
    """Get the path to the .machines config file"""
    return Path.home() / ".machines"


def get_active_api_key() -> str | None:
    """Get the currently active api key"""
    return config.active_api_key


def get_default_key_path() -> str:
    """Get the system's default SSH key path"""
    return config.default_ssh_key_path


def get_default_ssh_config_path() -> str:
    """Get the system's default SSH config path"""
    return config.ssh_config_path


def make_table_view(
    data: List[Dict[str, Any]], headers: List[str], tablefmt: str = "grid"
) -> str:
    # Filter out fields we don't want to display
    to_remove = ["id", "user_id", "created_at", "updated_at"]
    filtered_data = [
        {k: v for k, v in row.items() if k not in to_remove} for row in data
    ]

    # Filter headers to match filtered data
    filtered_headers = [h for h in headers if h not in to_remove]

    # Convert data to list of lists for tabulate
    table_data = []
    for row in filtered_data:
        table_data.append([row.get(header, "") for header in filtered_headers])

    return tabulate(table_data, headers=filtered_headers, tablefmt=tablefmt)


def add_to_ssh_config(machine_name: str, alias: str, port: int, user_id: str) -> None:
    """Add a machine to the SSH config"""
    # verify ssh config file
    ssh_config_file = config.ssh_config_path
    if not os.path.exists(ssh_config_file):
        # create the file
        with open(ssh_config_file, "w") as f:
            f.write("")

    # delete any existing machine from the ssh config
    remove_from_ssh_config(machine_name)

    if os.path.exists(ssh_config_file):
        with open(ssh_config_file, "a") as f:
            f.write(f"Host {machine_name}\n")
            f.write(f"    HostName {alias}\n")
            f.write(f"    User {user_id}\n")
            f.write(f"    Port {port}\n")
            f.write(f"    StrictHostKeyChecking no\n")
            f.write(f"    ForwardAgent yes\n")
            f.write(f"    ConnectTimeout 30\n")


def remove_from_ssh_config(machine_name: str) -> None:
    """Remove a machine from the SSH config"""
    ssh_config_file = config.ssh_config_path
    if os.path.exists(ssh_config_file):
        with open(ssh_config_file, "r") as f:
            lines = f.readlines()

        # Find and remove the entire config block
        i = 0
        while i < len(lines):
            if lines[i].startswith(f"Host {machine_name}"):
                # Remove the Host line
                lines.pop(i)
                # Remove all indented lines until we hit another Host or end of file
                while i < len(lines) and (
                    lines[i].startswith("    ") or lines[i].startswith("\t")
                ):
                    lines.pop(i)
                continue
            i += 1

        with open(ssh_config_file, "w") as f:
            f.writelines(lines)
