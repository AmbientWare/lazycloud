import typer

# Import command modules
from cli.commands.ssh.config import config_app
from cli.commands.ssh.keys import keys_app

# Create the ssh app
ssh_app = typer.Typer(help="SSH connection and configuration commands")

# Add command modules to the ssh app
ssh_app.add_typer(config_app, name="config")
ssh_app.add_typer(keys_app, name="keys")
