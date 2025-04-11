import typer

# Import command modules
from cli.commands.machines import machines_app
from cli.commands.ssh import ssh_app
from cli.commands.keys import keys_app
from cli.commands.volumes import volumes_app

# Create the main app
app = typer.Typer(
    name="lazycloud",
    help="LazyCloud CLI",
    add_completion=False,
)

# Add command modules to the main app
app.add_typer(machines_app, name="machines")
app.add_typer(ssh_app, name="ssh")
app.add_typer(keys_app, name="keys")
app.add_typer(volumes_app, name="volumes")

if __name__ == "__main__":
    app()
