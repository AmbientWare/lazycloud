import os

from lazycloud_cli.config import config


class SSHConfigManager:
    """Manages SSH configuration for cloud machines"""

    def __init__(self, config_path: str):
        self.config_path = config_path
        self._ensure_config_file()

    def _ensure_config_file(self) -> None:
        """Ensure the SSH config file exists"""
        if not os.path.exists(self.config_path):
            try:
                with open(self.config_path, "w") as f:
                    f.write("")
            except IOError as e:
                print(f"Failed to create SSH config file: {e}")
                raise

    def add_machine(self, machine_name: str, ip_address: str, port: int) -> None:
        """
        Add a machine configuration to the SSH config file.

        Args:
            machine_name: Name of the machine
            ip_address: IP address
            port: SSH port
        """
        self._ensure_config_file()
        self.remove_machine(machine_name)

        try:
            with open(self.config_path, "a") as f:
                f.write(
                    f"""Host {machine_name}
    HostName {ip_address}
    User ubuntu
    Port {port}
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    ForwardAgent yes
    ConnectTimeout 45
"""
                )
        except IOError as e:
            print(f"Failed to add machine to SSH config: {e}")
            raise

    def remove_machine(self, machine_name: str) -> None:
        """
        Remove a machine configuration from the SSH config file.

        Args:
            machine_name: Name of the machine to remove
        """
        if not os.path.exists(self.config_path):
            return

        try:
            with open(self.config_path, "r") as f:
                lines = f.readlines()

            with open(self.config_path, "w") as f:
                skip_lines = False
                for line in lines:
                    if line.startswith(f"Host {machine_name}"):
                        skip_lines = True
                        continue
                    if skip_lines and line.startswith("Host "):
                        skip_lines = False
                    if not skip_lines:
                        f.write(line)

        except IOError as e:
            print(f"Failed to remove machine from SSH config: {e}")
            raise

    def clear(self) -> None:
        """Clear all machine configurations from the SSH config file."""
        try:
            with open(self.config_path, "w") as f:
                f.write("")
        except IOError as e:
            print(f"Failed to clear SSH config: {e}")
            raise


# Create a global SSH config manager instance
ssh_config_manager = SSHConfigManager(config.ssh_config_path)
