"""
Restart command for compose deployments.
"""

from typing import Optional

import typer
from rich.console import Console
from rich.text import Text

from lazycloud_cli.api import api
from lazycloud_cli.ui.views.restart import RestartView
from lazycloud_cli.utils import get_current_deployment_name

console = Console()


def restart(
    deployment: Optional[str] = typer.Argument(None, help="Deployment ID or name"),
    service: Optional[str] = typer.Argument(None, help="Service name to restart"),
):
    """Restart services in a compose deployment."""
    if deployment and not service:
        current_deployment = get_current_deployment_name()

        if current_deployment:
            service = deployment
            deployment = current_deployment

        else:
            pass

    elif not deployment and not service:
        deployment = get_current_deployment_name()
        if not deployment:
            console.print(
                Text("No lazycloud.yaml found in current directory", style="red")
            )
            console.print(
                Text(
                    "Run from a directory with lazycloud.yaml or specify a deployment",
                    style="yellow",
                )
            )
            raise typer.Exit(1)

    try:
        # Get deployment by name or ID
        deployment_info = api.deployments.get_deployment(name=deployment)
        if not deployment_info:
            deployment_info = api.deployments.get_deployment(deployment_id=deployment)

        if not deployment_info:
            console.print(Text(f"Deployment '{deployment}' not fou  nd", style="red"))
            raise typer.Exit(1)

        view = RestartView()

        # Show restart initiation
        view.show_restart_initiation(deployment_info, service)

        if service:
            # Restart specific service
            restart_response = api.deployments.restart_service(
                deployment_info.id, service
            )
            view.show_restart_result(restart_response)

        else:
            # Restart all services
            restart_response = api.deployments.restart_all_services(deployment_info.id)
            view.show_restart_result(restart_response)

    except KeyboardInterrupt:
        console.print(Text("\nOperation cancelled", style="red"))
        raise typer.Exit(1)

    except Exception as e:
        console.print(Text(f"Error: {e}", style="red"))
        raise typer.Exit(1)
