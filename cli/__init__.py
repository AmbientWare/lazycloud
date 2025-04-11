# Import modules to register their commands
from cli.commands import app
import cli.commands.machines
import cli.commands.ssh
import cli.commands.volumes
import cli.commands.keys


__all__ = ["app"]
