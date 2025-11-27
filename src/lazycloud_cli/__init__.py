# Import modules to register their commands
import lazycloud_cli.commands.compose  # noqa: F401
import lazycloud_cli.commands.workspaces  # noqa: F401
from lazycloud_cli.commands import main_cli
from lazycloud_cli.utils import validate_cli_version

validate_cli_version()

__all__ = ["main_cli"]
