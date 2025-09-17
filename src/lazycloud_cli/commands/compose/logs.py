"""
Logs command for compose deployments.
"""

import asyncio
import select
import sys
import termios
import tty
from typing import Optional

import typer
from rich.console import Console
from rich.text import Text

from lazycloud_cli.api import api
from lazycloud_cli.ui.views.logs import LogViewer
from lazycloud_cli.utils import get_current_deployment_name

console = Console()

REFRESH_INTERVAL = 0.1
KEYBOARD_POLL_INTERVAL = 0.05
MOUSE_SCROLL_LINES = 3


def logs(
    deployment: Optional[str] = typer.Argument(None, help="Deployment ID or name"),
    service: Optional[str] = typer.Argument(None, help="Service name to get logs from"),
    no_follow: bool = typer.Option(
        False,
        "--no-follow",
        help="Don't follow log output (exit after showing existing logs)",
    ),
    tail: int = typer.Option(
        100, "-n", "--tail", help="Number of lines to show from the end"
    ),
):
    """Stream logs from a service in a compose deployment.

    DEPLOYMENT can be either a deployment ID or name. If not provided,
    uses the deployment from the current directory's lazycloud.yaml file.

    SERVICE is the name of the service to get logs from.

    Usage:
      lazycloud logs <service>              # Uses deployment from lazycloud.yaml
      lazycloud logs <deployment> <service> # Explicitly specify deployment
    """
    try:
        deployment, service = _resolve_deployment_and_service(deployment, service)
        deployment_info = _get_deployment_info(deployment)

        follow = not no_follow
        asyncio.run(_stream_logs(deployment_info.id, service, follow, tail))

    except KeyboardInterrupt:
        pass
    except Exception as e:
        console.print(Text(f"Error: {e}", style="red"))
        raise typer.Exit(1)


def _resolve_deployment_and_service(
    deployment: Optional[str], service: Optional[str]
) -> tuple[str, str]:
    if deployment and not service:
        service = deployment
        deployment = get_current_deployment_name()
        if not deployment:
            console.print(
                Text("No lazycloud.yaml found in current directory", style="red")
            )
            console.print(
                Text(
                    "Run from a directory with lazycloud.yaml or specify both deployment and service",
                    style="yellow",
                )
            )
            raise typer.Exit(1)
    elif not deployment and not service:
        console.print(Text("Service name is required", style="red"))
        console.print(
            Text(
                "Usage: lazycloud logs <service> or lazycloud logs <deployment> <service>",
                style="yellow",
            )
        )
        raise typer.Exit(1)
    return deployment, service


def _get_deployment_info(deployment: str):
    deployment_info = api.deployments.get_deployment(name=deployment)
    if not deployment_info:
        deployment_info = api.deployments.get_deployment(deployment_id=deployment)

    if not deployment_info:
        console.print(Text(f"Deployment '{deployment}' not found", style="red"))
        raise typer.Exit(1)

    return deployment_info


async def _stream_logs(deployment_id: str, service_name: str, follow: bool, tail: int):
    log_viewer = LogViewer(console, service_name, max_lines=1000)
    log_viewer._follow = follow

    def on_log_message(data: dict):
        if data and isinstance(data, dict):
            log_viewer.add_log_line(data.get("line"))

    def on_error(error: Exception):
        log_viewer.set_error(f"Connection error: {error}")

    try:
        with log_viewer.start():
            old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())

            refresh_task = asyncio.create_task(_periodic_refresh(log_viewer))
            keyboard_task = asyncio.create_task(_handle_keyboard(log_viewer))

            try:
                await api.logs.stream_logs(
                    deployment_id=deployment_id,
                    service_name=service_name,
                    tail=tail,
                    on_message=on_log_message,
                    on_error=on_error,
                )
            finally:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                refresh_task.cancel()
                keyboard_task.cancel()
                try:
                    await refresh_task
                    await keyboard_task
                except asyncio.CancelledError:
                    pass

    except Exception as e:
        console.print(Text(f"Failed to stream logs: {e}", style="red"))
        raise
    finally:
        log_viewer.stop()


async def _periodic_refresh(log_viewer: LogViewer):
    while log_viewer._live:
        await asyncio.sleep(REFRESH_INTERVAL)
        if log_viewer._live:
            try:
                log_viewer._live.update(log_viewer._render())
            except Exception:
                pass


async def _handle_keyboard(log_viewer: LogViewer):
    while log_viewer._live:
        try:
            rlist, _, _ = await asyncio.get_event_loop().run_in_executor(
                None, lambda: select.select([sys.stdin], [], [], 0.1)
            )

            if rlist:
                char = sys.stdin.read(1)
                _process_keyboard_input(char, log_viewer)
        except KeyboardInterrupt:
            raise
        except Exception:
            pass

        await asyncio.sleep(KEYBOARD_POLL_INTERVAL)


def _process_keyboard_input(char: str, log_viewer: LogViewer):
    if char == "\x1b":  # ESC sequence
        next_chars = sys.stdin.read(2)
        if next_chars == "[A":  # Up arrow
            log_viewer.scroll_up(MOUSE_SCROLL_LINES)
        elif next_chars == "[B":  # Down arrow
            log_viewer.scroll_down(MOUSE_SCROLL_LINES)
        elif next_chars == "[5":  # Page Up
            sys.stdin.read(1)  # consume ~
            log_viewer.page_up()
        elif next_chars == "[6":  # Page Down
            sys.stdin.read(1)  # consume ~
            log_viewer.page_down()
        elif next_chars == "[H":  # Home
            log_viewer.scroll_to_top()
        elif next_chars == "[F":  # End
            log_viewer.scroll_to_bottom()
    elif char == "a" or char == "A":
        log_viewer.scroll_to_bottom()
    elif char == " ":
        log_viewer.toggle_pause()
    elif char == "\x03":  # Ctrl+C
        raise KeyboardInterrupt()
