import time
import sys
import os
from pathlib import Path
from typing import List, Dict, Any
from tabulate import tabulate

from cli.config import config


class Spinner:
    def __init__(self, message: str = "Processing"):
        self.message = message
        self.spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self.spinner_index = 0
        self.start_time = None
        self.running = False

    def __enter__(self):
        self.start_time = time.time()
        self.running = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.running = False
        # Clear the spinner line
        sys.stdout.write("\r" + " " * (len(self.message) + 10) + "\r")
        sys.stdout.flush()

    def spin(self):
        if not self.running:
            return

        # Update spinner
        sys.stdout.write(f"\r{self.spinner_chars[self.spinner_index]} {self.message}")
        sys.stdout.flush()
        self.spinner_index = (self.spinner_index + 1) % len(self.spinner_chars)
        time.sleep(0.1)


def get_config_path() -> Path:
    """Get the path to the .machines config file"""
    return Path.home() / ".machines"


def get_active_token() -> str | None:
    """Get the currently active token"""
    return config.active_token


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
