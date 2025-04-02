import subprocess
from pathlib import Path
import os
import json
import sys
import asyncio

from machines.config import app_config
from machines.fly.schemas import (
    AppConfig,
    CheckStatus,
    FlyCommandError,
    FlyMachineConfig,
    RESOURCE_MAP,
)

NUM_CPU = os.cpu_count()
NUM_WORKERS = NUM_CPU // 2 if NUM_CPU else 1


class FlyAppManager:
    def __init__(self, org_name: str = app_config.FLY_ORG_NAME):
        """Initialize the Fly.io app manager.

        Args:
            org_name: The Fly.io organization name
            base_dir: The base directory for app files. Defaults to the directory containing this file.
        """
        self.org_name = org_name
        self.base_dir = Path(__file__).resolve().parent

    async def _get_app_name(self, name: str, user_id: str) -> str:
        """Get the name of the Fly.io application."""
        return f"{name}-{user_id}"

    async def _run_command(
        self, command: list[str], check: bool = True, print_output: bool = True
    ) -> subprocess.CompletedProcess:
        """Run a Fly.io command and handle errors asynchronously.

        Args:
            command: The command to run
            check: Whether to raise an exception if the command fails
            print_output: Whether to print the output in real-time
        """

        token_list = ["-t", app_config.FLY_API_TOKEN]

        if print_output:
            print(f"Running command: {' '.join(command)}")
        try:
            process = subprocess.Popen(
                command + token_list,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )

            if not process.stdout or not process.stderr:
                raise RuntimeError("Failed to create process pipes")

            if print_output:
                # Stream output in real-time
                output_lines = []
                error_lines = []
                while True:
                    output = process.stdout.readline()
                    error = process.stderr.readline()

                    if output:
                        output_lines.append(output.strip())
                        print(output.strip())
                    if error:
                        error_lines.append(error.strip())
                        print(error.strip(), file=sys.stderr)

                    if process.poll() is not None:
                        # Read any remaining output
                        remaining_output, remaining_error = process.communicate()
                        if remaining_output:
                            output_lines.extend(remaining_output.strip().split("\n"))
                            print(remaining_output.strip())
                        if remaining_error:
                            error_lines.extend(remaining_error.strip().split("\n"))
                            print(remaining_error.strip(), file=sys.stderr)
                        break

                return_code = process.returncode
                if check and return_code != 0:
                    error_msg = (
                        "\n".join(output_lines)
                        if output_lines
                        else "\n".join(error_lines) if error_lines else "Unknown error"
                    )
                    raise subprocess.CalledProcessError(
                        return_code,
                        command,
                        "\n".join(output_lines),
                        "\n".join(error_lines),
                    )

                return subprocess.CompletedProcess(
                    command,
                    return_code,
                    "\n".join(output_lines),
                    "\n".join(error_lines),
                )
            else:
                # For non-print mode, just use communicate() to get complete output
                stdout, stderr = process.communicate()
                return_code = process.returncode

                if check and return_code != 0:
                    raise subprocess.CalledProcessError(
                        return_code, command, stdout, stderr
                    )

                return subprocess.CompletedProcess(command, return_code, stdout, stderr)

        except subprocess.CalledProcessError as e:
            # Check both stdout and stderr for error messages
            error_msg = (
                e.stdout.strip()
                if e.stdout
                else e.stderr.strip() if e.stderr else "Unknown error"
            )
            # Extract just the actual error message, removing redundant wrapping
            if "failed to extend volume:" in error_msg:
                error_msg = error_msg.split("failed to extend volume:", 1)[1].strip()
            raise FlyCommandError(error_msg) from e

    async def _add_authorized_keys(self, config: AppConfig) -> None:
        """Add SSH authorized keys to the application."""
        pub_key = config.public_key.strip()
        if not pub_key:
            raise ValueError("SSH key file is empty")

        print(f"Adding secrets to app {config.name}")
        try:
            await self._run_command(
                [
                    "fly",
                    "secrets",
                    "set",
                    f"AUTHORIZED_KEYS={pub_key}",
                    "-a",
                    await self._get_app_name(config.name, config.user_id),
                ]
            )
        except subprocess.CalledProcessError as e:
            if "already exists" in e.stderr:
                print(f"Secret {config.name} already exists")
            else:
                raise e

    async def create_app(self, config: AppConfig) -> None:
        """Create a new Fly.io application."""
        print(f"Creating app {config.name}")
        # Create the app
        try:
            await self._run_command(
                [
                    "fly",
                    "apps",
                    "create",
                    await self._get_app_name(config.name, config.user_id),
                    "--org",
                    self.org_name,
                    "--network",
                    f"{config.name}-network",
                ]
            )
        except Exception as e:
            if "already been taken" in str(e):
                print(f"App {config.name} already exists")
            else:
                raise e

        try:
            # Allocate IPv4 address
            await self._run_command(
                [
                    "flyctl",
                    "ips",
                    "allocate-v4",
                    "--app",
                    await self._get_app_name(config.name, config.user_id),
                    "--yes",
                ]
            )
        except subprocess.CalledProcessError as e:
            if "already exists" in e.stderr:
                print(f"IPv4 address for app {config.name} already exists")
            else:
                # we made it this far, delete the app
                print(f"raising error: {e}")
                await self.delete_app(config.name, config.user_id)
                raise e

        # add the authorized key so that we can ssh into the machine
        await self._add_authorized_keys(config)

    async def delete_app(self, name: str, user_id: str) -> None:
        """Delete a Fly.io application."""
        print(f"Deleting app {name}")
        await self._run_command(
            ["fly", "apps", "destroy", await self._get_app_name(name, user_id), "--yes"]
        )

    async def deploy_app(
        self,
        machine_config: FlyMachineConfig,
        user_id: str,
    ) -> None:
        """Deploy the application to Fly.io."""
        print(f"Deploying app {machine_config.name}")

        fly_toml_path = self.base_dir / "app_files" / "fly.toml"
        # TODO: dynamically choose the dockerfile based on the image type
        dockerfile_path = self.base_dir / "docker_files" / "Dockerfile.ubuntu"

        await self._run_command(
            [
                "fly",
                "deploy",
                "--yes",
                "-a",
                await self._get_app_name(machine_config.name, user_id),
                "-c",
                str(fly_toml_path),
                "--dockerfile",
                str(dockerfile_path),
                "--primary-region",
                machine_config.region.value,
                "--vm-cpu-kind",
                machine_config.cpu_kind,
                "--vm-cpus",
                str(machine_config.cpu),
                "--vm-memory",
                str(machine_config.memory),
                "--volume-initial-size",
                str(machine_config.initial_volume_size),
                "--ha=false",
            ]
        )

    async def scale_app(
        self, name: str, user_id: str, cpu_kind: str, cpu: int, memory: int
    ) -> None:
        """Scale the application to the given number of machines."""
        print(f"Scaling app {name} to {cpu_kind} {cpu} {memory}")
        vm_type = RESOURCE_MAP.get(cpu_kind)
        if not vm_type:
            raise ValueError(
                f"Invalid CPU kind: {cpu_kind}. Must be one of {RESOURCE_MAP.keys()}"
            )

        vm_config = vm_type.get(cpu)
        if not vm_config:
            raise ValueError(
                f"Invalid CPU size: {cpu} for kind: {cpu_kind}. Must be one of {vm_type.keys()}"
            )

        if memory not in vm_config["memory_options"]:
            memory_gb = memory / 1024
            valid_memory_gb = [f"{opt/1024:.1f}" for opt in vm_config["memory_options"]]
            raise ValueError(
                f"Invalid memory size: {memory_gb:.2f}GB for {vm_config['name']}. "
                f"Must be one of {', '.join(valid_memory_gb)}GB"
            )

        await self._run_command(
            [
                "fly",
                "scale",
                "vm",
                vm_config["name"],
                "-a",
                await self._get_app_name(name, user_id),
                "--vm-memory",
                str(memory),
            ]
        )

    async def extend_volume(self, name: str, user_id: str, volume_size: int) -> None:
        """Extend the volume of the application."""
        print(f"Extending volume of app {name} to {volume_size}GB")

        try:
            # get the volume id
            response = await self._run_command(
                [
                    "fly",
                    "volume",
                    "list",
                    "-a",
                    await self._get_app_name(name, user_id),
                    "--json",
                ],
                print_output=False,
            )

            volumes = json.loads(response.stdout)
            for volume in volumes:
                if volume["name"] == "persistant_data":
                    volume_id = volume["id"]
                    break

            if not volume_id:
                raise ValueError(f"Volume {name} not found")

        except subprocess.CalledProcessError as e:
            raise e

        try:
            await self._run_command(
                [
                    "fly",
                    "volume",
                    "extend",
                    volume_id,
                    "-s",
                    str(volume_size),
                    "-a",
                    await self._get_app_name(name, user_id),
                ]
            )

        except subprocess.CalledProcessError as e:
            raise e

    async def get_check_status(self, name: str, user_id: str) -> CheckStatus:
        """Get the status of the check for the application."""
        app_name = await self._get_app_name(name, user_id)
        response = await self._run_command(
            [
                "fly",
                "machines",
                "list",
                "-a",
                app_name,
                "--json",
            ],
            print_output=False,
        )
        machines = json.loads(response.stdout)
        for machine in machines:
            checks = machine.get("checks", {})
            ssh_check = checks.get("sshCheck", {})
            if ssh_check.get("status") == "passing":
                return CheckStatus.PASSING

            elif ssh_check.get("status") == "failing":
                return CheckStatus.FAILING

            else:
                return CheckStatus.UNKNOWN

        return CheckStatus.UNKNOWN
