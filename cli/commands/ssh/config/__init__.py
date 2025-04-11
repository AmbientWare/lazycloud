import typer

# Import command modules
from cli.commands.ssh.config.show import app as show_app
from cli.commands.ssh.config.clear import app as clear_app

# Create the config app
config_app = typer.Typer(help="SSH configuration commands")

# Add command modules to the config app
config_app.add_typer(show_app)
config_app.add_typer(clear_app)
