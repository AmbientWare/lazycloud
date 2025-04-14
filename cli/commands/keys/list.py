import typer
from rich.table import Table
from cli.config import config
from cli.logging import logger

app = typer.Typer(help="List API keys")

@app.command()
def ls():
    """List all API keys"""
    try:
        keys = config.list_api_keys()
        active_key = config.active_api_key

        if not keys:
            logger.info("No API keys found")
            return

        # creat data for table
        data = []
        for name, value in keys.items():
            status = "Active" if name == active_key else ""
            data.append({"name": name, "value": value, "status": status})

        logger.table(data)

    except Exception as e:
        logger.error(f"Failed to list API keys: {e}")
        raise typer.Exit(1)
