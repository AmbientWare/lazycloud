"""
Service status command for detailed service information.
"""

from typing import Optional

import typer
from rich.console import Console

from lazycloud_cli.api import api
from lazycloud_cli.ui.views.service import ServiceStatusApp, ServiceView
from lazycloud_cli.utils import get_current_deployment_name

console = Console()


def status(
    service: str = typer.Argument(..., help="Service name"),
    deployment: Optional[str] = typer.Argument(None, help="Deployment ID or name"),
):
    """Get detailed real-time status of a specific service.

    SERVICE is the name of the service to monitor.
    DEPLOYMENT can be either a deployment ID or name. If not provided,
    uses the deployment from the current directory's lazycloud.yaml file.

    The status will automatically update in real-time as changes occur.

    Examples:
        lazycloud service status api
        lazycloud service status nginx production-app
    """
    view = ServiceView(console)

    # If no deployment specified, try to infer from current directory
    if not deployment:
        deployment = get_current_deployment_name()
        if not deployment:
            view.show_error(
                "No deployment specified and no lazycloud.yaml found in current directory"
            )
            console.print(
                "\n💡 Specify a deployment name or run from a directory with lazycloud.yaml"
            )
            raise typer.Exit(1)

    try:
        # First try to get deployment by name or ID
        deployment_info = None
        try:
            deployment_info = api.deployments.get_deployment(name=deployment)
        except Exception:
            pass

        if not deployment_info:
            try:
                deployment_info = api.deployments.get_deployment(
                    deployment_id=deployment
                )
            except Exception:
                pass

        if not deployment_info:
            console.print(
                f"[red]Deployment '{deployment}' not found.[/red]\n"
                f"💡 Use 'lazycloud list' to see available deployments."
            )
            raise typer.Exit(1)

        # Create and run Textual app for scrollable service status
        app = ServiceStatusApp(
            deployment_id=deployment_info.id,
            service_name=service,
            deployment_info=deployment_info.model_dump(),
        )
        app.run()

    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully
        console.print("\n[dim]Stopped monitoring.[/dim]")
    except Exception as e:
        view.show_error(str(e))
        raise typer.Exit(1)
