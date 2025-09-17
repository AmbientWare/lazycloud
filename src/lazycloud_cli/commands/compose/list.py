"""
List command for compose deployments.
"""

import typer
from rich.console import Console

from lazycloud_cli.api import api
from lazycloud_cli.ui.views import ListView

console = Console()


def list_deployments(
    status: str = typer.Option(None, "--status", "-s", help="Filter by status"),
    limit: int = typer.Option(
        10, "--limit", "-l", help="Maximum number of deployments to show"
    ),
):
    """List compose deployments."""
    view = ListView(console)

    try:
        response = api.deployments.list_deployments(status=status, limit=limit)

        # Convert deployment objects to dictionaries for the view
        deployments = []
        for dep in response.deployments:
            deployments.append(
                {
                    "id": dep.id,
                    "name": dep.name,
                    "status": dep.status,
                    "created_at": dep.created_at,
                }
            )

        # Show deployments using the view
        view.show_deployments(
            deployments=deployments,
            total=response.total,
            filters={"status": status} if status else None,
        )

    except Exception as e:
        view.show_error(f"Failed to fetch deployments: {str(e)}")
        raise typer.Exit(1)
