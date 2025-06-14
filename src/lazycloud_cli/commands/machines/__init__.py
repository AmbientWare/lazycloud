import typer

# Import command modules
from lazycloud_cli.commands.machines.create import app as create_app
from lazycloud_cli.commands.machines.scale import app as scale_app
from lazycloud_cli.commands.machines.destroy import app as destroy_app
from lazycloud_cli.commands.machines.get import app as get_app
from lazycloud_cli.commands.machines.list import app as list_app
from lazycloud_cli.commands.machines.connect import app as connect_app
from lazycloud_cli.commands.machines.options import app as options_app
from lazycloud_cli.commands.machines.auto_stop import app as auto_stop_app
from lazycloud_cli.commands.machines.keep_alive import app as keep_alive_app
from lazycloud_cli.commands.machines.restart import app as restart_app
from lazycloud_cli.commands.machines.extend_size import app as extend_size_app

# Create the machines app
machine_app = typer.Typer(help="Machine management commands")

# Add command modules to the machines app
machine_app.add_typer(create_app)
machine_app.add_typer(scale_app)
machine_app.add_typer(destroy_app)
machine_app.add_typer(get_app)
machine_app.add_typer(list_app)
machine_app.add_typer(connect_app)
machine_app.add_typer(options_app)
machine_app.add_typer(auto_stop_app)
machine_app.add_typer(keep_alive_app)
machine_app.add_typer(restart_app)
machine_app.add_typer(extend_size_app)
