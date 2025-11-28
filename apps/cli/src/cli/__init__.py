# Import modules to register their commands
import cli.commands.compose  # noqa: F401
import cli.commands.workspaces  # noqa: F401
from cli.commands import main_cli
from cli.utils import validate_cli_version

validate_cli_version()

__all__ = ["main_cli"]
