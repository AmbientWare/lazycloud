# Configure logging first (before other imports may use logger)
from cli.utils.logging import configure_logging

configure_logging()

# Import modules to register their commands
import cli.commands.compose  # noqa: F401
import cli.commands.workspaces  # noqa: F401
from cli.commands import main_cli
from cli.utils import validate_cli_version

validate_cli_version()

__all__ = ["main_cli"]
