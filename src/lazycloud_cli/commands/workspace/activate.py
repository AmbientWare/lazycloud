import typer
from rich.console import Console

from lazycloud_cli.api import api
from lazycloud_cli.config import config
from lazycloud_cli.ui.views import WorkspaceView

app = typer.Typer(help="Activate a workspace")
console = Console()


@app.command()
def activate(
    name: str = typer.Argument(..., help="Name of the workspace to activate"),
):
    """Activate a workspace as the current workspace"""
    view = WorkspaceView(console)

    try:
        workspace = api.workspaces.get_workspace_by_name(name)

        if not workspace:
            view.show_not_found(name)
            raise typer.Exit(1)

        config.set_active_workspace(workspace["id"], workspace["name"])
        view.show_activated(workspace)

    except typer.Exit:
        raise

    except Exception as e:
        view.show_error(f"Failed to activate workspace: {e}")
        raise typer.Exit(1)
