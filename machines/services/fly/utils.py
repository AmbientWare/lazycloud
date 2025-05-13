# move here

import asyncio
import json
import subprocess
import sys
from typing import List, Callable, Awaitable
from loguru import logger

from machines.config import app_config
from machines.services.fly.schemas import FlyCommandError
from machines.database.machines import MachineStatus
from machines.database import db


async def get_app_name(usage_uuid: str) -> str:
    """Get the name of the Fly.io application."""
    return f"lc-{usage_uuid}"


async def get_machine_name(machine_id: int) -> str:
    """Get the name of the Fly.io machine."""
    return f"lc_machine_{machine_id}"


async def get_app_volume_name(file_system_id: int) -> str:
    """Get the name of the Fly.io application volume."""
    return f"lc_volume_{file_system_id}"


async def get_app_ipv4(usage_uuid: str) -> str | None:
    """Get the name of the Fly.io application IPv4."""
    app_name = await get_app_name(usage_uuid)
    cmd = ["fly", "ips", "list", "-a", app_name, "--json"]

    response = await run_async_command(cmd, print_output=False)
    ips = json.loads(response.stdout)

    if len(ips) == 0:
        return None

    # each app will only have one ipv4
    return ips[0].get("Address")


async def get_fly_volume_id(
    usage_uuid: str, file_system_id: int | None = None, volume_name: str | None = None
) -> str | None:
    """Get the volume id for the application."""
    if file_system_id is None and volume_name is None:
        raise ValueError("Either file_system_id or volume_name must be provided")

    elif volume_name is None:
        if file_system_id is None:
            raise ValueError("file_system_id must be provided if volume_name is not")

        volume_name = await get_app_volume_name(file_system_id)

    response = await run_async_command(
        [
            "fly",
            "volume",
            "list",
            "-a",
            await get_app_name(usage_uuid),
            "--json",
        ],
        print_output=False,
    )

    volumes = json.loads(response.stdout)
    for volume in volumes:
        if (
            volume.get("name") == volume_name
            and volume.get("status") != "pending_destroy"
        ):
            logger.info(f"Found volume {volume_name} with id {volume.get('id')}")
            return volume.get("id")

    return None


async def deploying_status_callback(machine_id: int, stdout_line: str) -> None:
    lower_line = stdout_line.lower()
    """Update the status of the machine as it is being deployed."""
    if "building image" in lower_line:
        await db.machines.update_machine_status(machine_id, MachineStatus.BUILDING)
    elif "volume named" in lower_line:
        await db.machines.update_machine_status(machine_id, MachineStatus.VOLUME)
    elif "launching new machine" in lower_line:
        await db.machines.update_machine_status(machine_id, MachineStatus.VM_CREATING)


async def run_async_command(
    command: List[str],
    check: bool = True,
    print_output: bool = True,
    stdout_callback: Callable[[str], Awaitable[None]] | None = None,
) -> subprocess.CompletedProcess:
    """Run a command asynchronously and handle errors.

    Args:
        command: The command to run
        check: Whether to raise an exception if the command fails
        print_output: Whether to print the output in real-time
    """
    fly_token_list = ["-t", app_config.FLY_API_TOKEN]
    full_command = command + fly_token_list

    if print_output:
        logger.info(f"Running command: {' '.join(full_command)}")

    try:
        # Create the subprocess asynchronously
        process = await asyncio.create_subprocess_exec(
            *full_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        if not process.stdout or not process.stderr:
            raise RuntimeError("Failed to create process pipes")

        if print_output:
            # Stream output in real-time
            output_lines = []
            error_lines = []

            async def read_stream(stream, is_error=False):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    # Decode the bytes to string
                    line = line.decode("utf-8", errors="replace").strip()
                    if stdout_callback:
                        await stdout_callback(line)

                    if is_error:
                        error_lines.append(line)
                        print(line, file=sys.stderr)
                    else:
                        output_lines.append(line)
                        print(line)

            # Read stdout and stderr concurrently
            await asyncio.gather(
                read_stream(process.stdout), read_stream(process.stderr, True)
            )

            # Wait for the process to complete
            return_code = await process.wait()

            if check and return_code != 0:
                error_msg = (
                    "\n".join(output_lines)
                    if output_lines
                    else "\n".join(error_lines) if error_lines else "Unknown error"
                )
                raise subprocess.CalledProcessError(
                    return_code,
                    full_command,
                    "\n".join(output_lines),
                    "\n".join(error_lines),
                )

            return subprocess.CompletedProcess(
                full_command,
                return_code,
                "\n".join(output_lines),
                "\n".join(error_lines),
            )
        else:
            # For non-print mode, just use communicate() to get complete output
            stdout, stderr = await process.communicate()
            return_code = await process.wait()

            # Decode the output
            stdout = stdout.decode("utf-8", errors="replace") if stdout else ""
            stderr = stderr.decode("utf-8", errors="replace") if stderr else ""

            if check and return_code != 0:
                raise subprocess.CalledProcessError(
                    return_code, full_command, stdout, stderr
                )

            return subprocess.CompletedProcess(
                full_command, return_code, stdout, stderr
            )

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
