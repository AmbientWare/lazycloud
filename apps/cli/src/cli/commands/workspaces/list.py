import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from cli.api import api
from cli.config import config
from cli.ui.colors import Colors
from cli.ui.components.card import Card

app = typer.Typer(help="List workspaces")
console = Console()


@app.command()
def list():
    """List all workspaces"""
    try:
        workspaces = api.workspaces.list_workspaces()

        if not workspaces:
            content = Text("No workspaces found", style=Colors.Ansi.text_muted)
            card = Card(
                content=content,
                title="🗂️  Workspaces",
                border_style=Colors.Ansi.warning,
            )
            console.print(card)
            return

        table = Table(
            show_header=True, header_style=f"bold {Colors.Ansi.primary}", box=None
        )
        table.add_column("Name", style=Colors.Ansi.primary)
        table.add_column("Role", style=Colors.Ansi.text_muted)
        table.add_column("Status", style=Colors.Ansi.success)

        active_workspace_id = config.active_workspace_id

        for ws in workspaces:
            name = ws.get("name", "")
            role = ws.get("role", "")
            status = "✓ Active" if ws.get("id") == active_workspace_id else ""

            table.add_row(name, role, status)

        card = Card(
            content=table,
            title="🗂️  Workspaces",
            subtitle=f"{len(workspaces)} workspace{'s' if len(workspaces) != 1 else ''} configured",
            border_style=Colors.Ansi.primary,
        )
        console.print(card)

    except Exception as e:
        error_card = Card(
            content=Text(f"Failed to list workspaces: {e}", style=Colors.Ansi.error),
            title="🗂️  Error",
            border_style=Colors.Ansi.error,
        )
        console.print(error_card)
        raise typer.Exit(1)
