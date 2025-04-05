import os

from cli.config import config


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

    def add_machine(
        self, machine_name: str, alias: str, port: int, user_id: str
    ) -> None:
        """
        Add a machine configuration to the SSH config file.

        Args:
            machine_name: Name of the machine
            alias: Hostname or IP address
            port: SSH port
            user_id: SSH user ID
        """
        self._ensure_config_file()
        self.remove_machine(machine_name)

        try:
            with open(self.config_path, "a") as f:
                f.write(
                    f"""Host {machine_name}
    HostName {alias}
    User {user_id}
    Port {port}
    StrictHostKeyChecking no
    ForwardAgent yes
    ConnectTimeout 30
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

            with open(self.config_path, "w") as f:
                f.writelines(lines)
        except IOError as e:
            print(f"Failed to remove machine from SSH config: {e}")
            raise


# Create a global SSH config manager instance
ssh_config_manager = SSHConfigManager(config.ssh_config_path)
