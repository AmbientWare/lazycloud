import typer
from rich.console import Console

from cli.api import api
from cli.config import config
from cli.ui.views import WorkspaceView

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

        # Check if this is the active workspace before deleting
        was_active = config._active_workspace_id == workspace["id"]

        # Delete workspace
        api.workspaces.delete_workspace(workspace["id"])
        view.show_removed(name)

        # If we destroyed the active workspace, activate Personal as fallback
        if was_active:
            personal = api.workspaces.get_workspace_by_name("Personal")
            if personal:
                config.set_active_workspace(personal["id"], personal["name"])

    except typer.Exit:
        raise

    except Exception as e:
        view.show_error(f"Failed to remove workspace: {e}")
        raise typer.Exit(1)
