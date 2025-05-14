import typer

# Import command modules
from lazycloud_cli.commands.file_systems.extend import app as extend_app
from lazycloud_cli.commands.file_systems.create import app as create_app
from lazycloud_cli.commands.file_systems.destroy import app as destroy_app
from lazycloud_cli.commands.file_systems.list import app as list_app
from lazycloud_cli.commands.file_systems.duplicate import app as duplicate_app

# Create the file systems app
fs_app = typer.Typer(help="File system management commands")

# Add command modules to the file systems app
fs_app.add_typer(extend_app)
fs_app.add_typer(create_app)
fs_app.add_typer(destroy_app)
fs_app.add_typer(list_app)
fs_app.add_typer(duplicate_app)
