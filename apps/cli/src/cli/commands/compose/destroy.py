import asyncio

import typer
from models.statuses import TaskStatus
from rich.console import Console

from cli.api import APIError, api
from cli.ui.components.info_cards import SubscriptionRequiredCard
from cli.ui.views import DestroyView
from cli.utils import get_current_deployment_name

console = Console()


def destroy(
    name: str | None = typer.Argument(
        None,
        help="Deployment name to destroy (use 'lazycloud compose list' to see available deployments)",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt"),
):
    """Destroy a Docker Compose deployment.

    If no deployment name is provided, uses the deployment from the current
    directory's lazycloud.yaml file.
    """
    view = DestroyView(console)

    # If no name specified, try to infer from current directory
    if not name:
        name = get_current_deployment_name()
        if not name:
            view.show_error(
                "No deployment specified and no lazycloud.yaml found in current directory",
            )
            raise typer.Exit(1)

    try:
        # Get deployment details
        deployment = api.deployments.get_deployment(name=name)

        if not deployment:
            view.show_error(f"Deployment '{name}' not found")
            raise typer.Exit(1)

        # Show target deployment
        deployment_data = {
            "id": deployment.id,
            "name": deployment.name,
        }
        view.show_target(deployment_data)

        # Confirm destruction
        if not view.confirm_destruction(deployment.name, force):
            view.show_cancelled()
            return

        # Delete via API with streaming (no polling!)
        with view.show_progress(deployment.name):
            delete_response = asyncio.run(
                api.deployments.delete_deployment(deployment_id=deployment.id)
            )

        if delete_response.status == TaskStatus.COMPLETED:
            view.show_success(deployment.name)
        else:
            view.show_error(f"Destruction failed: {delete_response.message}")
            raise typer.Exit(1)

    except typer.Exit:
        # Re-raise Exit exceptions
        raise
    except APIError as e:
        if e.status_code == 402:
            console.print(SubscriptionRequiredCard())
        else:
            view.show_error(f"API error: {str(e)}")
        raise typer.Exit(1)
    except Exception as e:
        view.show_error(f"Unexpected error: {str(e)}")
        raise typer.Exit(1)
