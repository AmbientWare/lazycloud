import typer

# Import command modules
from cli.commands.ssh.keys.list import app as list_app
from cli.commands.ssh.keys.add import app as add_app
from cli.commands.ssh.keys.remove import app as remove_app

# Create the keys app
keys_app = typer.Typer(help="SSH key management")

# Add command modules to the keys app
keys_app.add_typer(list_app)
keys_app.add_typer(add_app)
keys_app.add_typer(remove_app)
