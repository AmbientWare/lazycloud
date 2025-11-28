import typer
from rich.console import Console

from cli.api import api
from cli.ui.views import WorkspaceView

app = typer.Typer(help="Create a new workspace")
console = Console()


@app.command()
def create(
    name: str = typer.Argument(..., help="Name of the workspace to create"),
):
    """Create a new workspace"""
    view = WorkspaceView(console)

    try:
        view.show_creating(name)
        workspace = api.workspaces.create_workspace(name)
        view.show_created(workspace)

    except typer.Exit:
        raise
    except Exception as e:
        view.show_error(f"Failed to create workspace: {e}")
        raise typer.Exit(1)
