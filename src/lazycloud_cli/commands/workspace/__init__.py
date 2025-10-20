import typer

from lazycloud_cli.commands.workspace.activate import app as activate_app
from lazycloud_cli.commands.workspace.create import app as create_app
from lazycloud_cli.commands.workspace.destroy import app as destroy_app
from lazycloud_cli.commands.workspace.list import app as list_app

workspace_app = typer.Typer(help="Workspace management commands")

workspace_app.add_typer(activate_app)
workspace_app.add_typer(create_app)
workspace_app.add_typer(list_app)
workspace_app.add_typer(destroy_app)
