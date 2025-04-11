import typer
from cli.logging import logger
from cli.ssh_config import ssh_config_manager


app = typer.Typer(help="Clear SSH configuration")


@app.command()
def clear():
    """Clear SSH configuration"""
    try:
        if not typer.confirm("Are you sure you want to clear the SSH configuration?"):
            return

        ssh_config_manager.clear()
        logger.success("Cleared SSH configuration")

    except Exception as e:
        logger.error(f"Failed to clear SSH configuration: {e}")
        raise typer.Exit(1)
