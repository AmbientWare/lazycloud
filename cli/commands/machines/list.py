import typer

from cli.api import api
from cli.logging import logger

app = typer.Typer(help="List machines")


@app.command()
def ls(
    status: str = typer.Option(
        None, "--status", "-s", help="Filter machines by status"
    ),
    region: str = typer.Option(
        None, "--region", "-r", help="Filter machines by region"
    ),
    type: str = typer.Option(None, "--type", "-t", help="Filter machines by type"),
):
    """List all machines with optional filtering"""
    try:
        # Get machines from API
        machines = api.machines.list_machines()

        # Apply filters if specified
        if status:
            machines = [m for m in machines if m["status"] == status]
        if region:
            machines = [m for m in machines if m["region"] == region]
        if type:
            machines = [m for m in machines if m["type"] == type]

        if not machines:
            logger.info("No machines found")
            return

        # Display the table
        logger.table(machines)

    except Exception as e:
        logger.error(f"Failed to list machines: {e}")
        raise typer.Exit(1)
