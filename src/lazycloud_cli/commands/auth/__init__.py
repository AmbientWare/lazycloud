import typer

# Import command modules
from lazycloud_cli.commands.auth.add import app as add_app
from lazycloud_cli.commands.auth.list import app as list_app
from lazycloud_cli.commands.auth.remove import app as remove_app
from lazycloud_cli.commands.auth.set import app as set_app

# Create the keys app
auth_app = typer.Typer(help="Auth with API key management commands")

# Add command modules to the keys app
auth_app.add_typer(add_app)
auth_app.add_typer(list_app)
auth_app.add_typer(remove_app)
auth_app.add_typer(set_app)
