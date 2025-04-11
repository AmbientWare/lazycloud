import typer

# Import command modules
from cli.commands.volumes.extend import app as extend_app

# Create the volumes app
volumes_app = typer.Typer(help="Volume management commands")

# Add command modules to the volumes app
volumes_app.add_typer(extend_app)
