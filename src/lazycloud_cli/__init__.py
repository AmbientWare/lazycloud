# Import modules to register their commands
from lazycloud_cli.commands import cli
import lazycloud_cli.commands.machines
import lazycloud_cli.commands.ssh
import lazycloud_cli.commands.auth


__all__ = ["cli"]
