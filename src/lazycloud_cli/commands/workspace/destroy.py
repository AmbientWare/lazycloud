import typer
from rich.console import Console

from lazycloud_cli.api import api
from lazycloud_cli.ui.views import WorkspaceView

app = typer.Typer(help="Destroy a workspace")
console = Console()


@app.command()
def destroy(
    name: str = typer.Argument(..., help="Name of the workspace to destroy"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """Destroy a workspace"""
    view = WorkspaceView(console)

    try:
        # Find workspace by name
        workspace = api.workspaces.get_workspace_by_name(name)

        if not workspace:
            view.show_not_found(name)
            raise typer.Exit(1)

        # Check if it's personal workspace
        if workspace.get("is_personal"):
            view.show_personal_cannot_remove()
            raise typer.Exit(1)

        # Confirm deletion
        if not view.confirm_removal(name, force):
            view.show_cancelled()
            raise typer.Exit(0)

        # Delete workspace
        api.workspaces.delete_workspace(workspace["id"])
        view.show_removed(name)

    except typer.Exit:
        raise

    except Exception as e:
        view.show_error(f"Failed to remove workspace: {e}")
        raise typer.Exit(1)
