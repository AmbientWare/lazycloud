import typer
from pathlib import Path
from cli.config import config
from cli.logging import logger


app = typer.Typer(help="Show SSH configuration")


@app.command()
def show():
    """Show current SSH configuration"""
    try:
        config_path = Path(config.ssh_config_path)
        if not config_path.exists():
            logger.info("No SSH configuration file found")
            return

        with open(config_path) as f:
            content = f.read()
            if not content.strip():
                logger.info("SSH configuration file is empty")
                return

            logger.info("Current SSH configuration:")
            logger.info(content)

    except Exception as e:
        logger.error(f"Failed to show SSH configuration: {e}")
        raise typer.Exit(1)
