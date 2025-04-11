import typer

# Import command modules
from cli.commands.keys.add import app as add_app
from cli.commands.keys.list import app as list_app
from cli.commands.keys.remove import app as remove_app
from cli.commands.keys.set_active import app as set_active_app

# Create the keys app
keys_app = typer.Typer(help="API key management commands")

# Add command modules to the keys app
keys_app.add_typer(add_app)
keys_app.add_typer(list_app)
keys_app.add_typer(remove_app)
keys_app.add_typer(set_active_app)
