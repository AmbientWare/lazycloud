from datetime import datetime, timezone

import typer
from models.deployments import DeploymentStates
from rich.console import Console
from rich.table import Table
from rich.text import Text

from cli.api import api
from cli.ui.colors import Colors
from cli.ui.components.card import Card
from cli.ui.textual.theme import Symbols

app = typer.Typer(help="List deployments")
console = Console()


def _get_state_style(state: DeploymentStates) -> tuple[str, str]:
    """Return (color, symbol) for a deployment state."""
    styles = {
        DeploymentStates.DEPLOYED: (Colors.Ansi.success, Symbols.CIRCLE_FILLED),
        DeploymentStates.DEPLOYING: (Colors.Ansi.warning, Symbols.CIRCLE_HALF),
        DeploymentStates.PENDING: (Colors.Ansi.warning, Symbols.CIRCLE_EMPTY),
        DeploymentStates.FAILED: (Colors.Ansi.error, Symbols.CROSS),
        DeploymentStates.DELETING: (Colors.Ansi.warning, Symbols.CIRCLE_HALF),
        DeploymentStates.DELETED: (Colors.Ansi.text_muted, Symbols.CIRCLE_EMPTY),
    }
    return styles.get(state, (Colors.Ansi.text_muted, Symbols.CIRCLE_EMPTY))


def _format_relative_time(dt) -> str:
    """Format datetime as relative time string."""
    if not dt:
        return "-"

    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    delta = now - dt

    if delta.days > 0:
        return f"{delta.days}d ago"
    elif delta.seconds > 3600:
        return f"{delta.seconds // 3600}h ago"
    elif delta.seconds > 60:
        return f"{delta.seconds // 60}m ago"
    else:
        return "just now"


@app.command()
def list(
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum deployments to show"),
    all_states: bool = typer.Option(
        False, "--all", "-a", help="Show all deployments including deleted"
    ),
):
    """List all deployments in the active workspace."""
    try:
        response = api.deployments.list_deployments(limit=limit)
        deployments = response.deployments

        # Filter out deleted by default
        if not all_states:
            deployments = [
                d for d in deployments if d.state != DeploymentStates.DELETED
            ]

        if not deployments:
            content = Text("No deployments found", style=Colors.Ansi.text_muted)
            card = Card(
                content=content,
                title="🚀  Deployments",
                border_style=Colors.Ansi.warning,
            )
            console.print(card)
            return

        table = Table(
            show_header=True,
            header_style=f"bold {Colors.Ansi.primary}",
            box=None,
        )
        table.add_column("", width=2)  # Status indicator
        table.add_column("Name", style=Colors.Ansi.primary)
        table.add_column("State", style=Colors.Ansi.text_muted)
        table.add_column("Deployed", style=Colors.Ansi.text_muted)

        for dep in deployments:
            color, symbol = _get_state_style(dep.state)
            status_dot = Text(symbol, style=color)
            state_text = Text(dep.state.value.capitalize(), style=color)
            deployed_time = _format_relative_time(dep.deployed_at)

            table.add_row(
                status_dot,
                dep.name,
                state_text,
                deployed_time,
            )

        total_info = (
            f"{len(deployments)} deployment{'s' if len(deployments) != 1 else ''}"
        )
        if response.has_more:
            total_info += f" (showing {limit} of {response.total})"

        card = Card(
            content=table,
            title="🚀  Deployments",
            subtitle=total_info,
            border_style=Colors.Ansi.primary,
        )
        console.print(card)

    except Exception as e:
        error_card = Card(
            content=Text(f"Failed to list deployments: {e}", style=Colors.Ansi.error),
            title="🚀  Error",
            border_style=Colors.Ansi.error,
        )
        console.print(error_card)
        raise typer.Exit(1)
