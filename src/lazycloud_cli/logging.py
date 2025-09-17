# rich logging
from enum import StrEnum

from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
)
from rich.theme import Theme

REMOVABLE_TABLE_NAMES = [
    "id",
    "created_at",
    "updated_at",
    "user_id",
]


# Define a custom theme for consistent styling
custom_theme = Theme(
    {
        "info": "white",
        "success": "bright_green",
        "status": "bright_yellow",
        "warning": "yellow",
        "error": "bright_red",
        "debug": "bright_blue",
        "highlight": "bright_cyan",
        "accent": "bright_magenta",
        "muted": "dim white",
        "header": "bold bright_white on deep_sky_blue4",
        "subheader": "bold bright_cyan",
    }
)


class LogLevel(StrEnum):
    """Log levels with their corresponding styles"""

    INFO = "info"
    SUCCESS = "success"
    STATUS = "status"
    WARNING = "warning"
    ERROR = "error"
    DEBUG = "debug"


class Logger:
    """A logger that uses Rich for terminal output"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.console = Console(theme=custom_theme)
        self.error_console = Console(theme=custom_theme, stderr=True)

    def _log(
        self, level: LogLevel, message: str, bold: bool = False, icon: str | None = None
    ) -> None:
        """Internal logging method that handles styling"""
        # Map log levels to actual colors for bold styling
        level_colors = {
            LogLevel.INFO: "white",
            LogLevel.SUCCESS: "bright_green",
            LogLevel.STATUS: "bright_yellow",
            LogLevel.WARNING: "yellow",
            LogLevel.ERROR: "bright_red",
            LogLevel.DEBUG: "bright_blue",
        }

        if bold:
            # Use actual color name when bold is True
            base_color = level_colors.get(level, "white")
            style = f"bold {base_color}"
        else:
            # Use theme style when bold is False
            style = level.value

        # Add icons for different log levels
        if icon is None:
            icons = {
                LogLevel.INFO: "ℹ️ ",
                LogLevel.SUCCESS: "✅",
                LogLevel.STATUS: "⏳",
                LogLevel.WARNING: "⚠️ ",
                LogLevel.ERROR: "❌",
                LogLevel.DEBUG: "🔍",
            }
            icon = icons.get(level, "")

        formatted_message = f"{icon} {message}" if icon else message

        # Use error console for errors
        if level == LogLevel.ERROR:
            self.error_console.print(formatted_message, style=style)
        else:
            self.console.print(formatted_message, style=style)

    def success(
        self, message: str, bold: bool = False, icon: str | None = None
    ) -> None:
        """Log a success message"""
        self._log(LogLevel.SUCCESS, message, bold, icon)

    def status(self, message: str, bold: bool = False, icon: str | None = None) -> None:
        """Log a status message"""
        self._log(LogLevel.STATUS, message, bold, icon)

    def warning(
        self, message: str, bold: bool = False, icon: str | None = None
    ) -> None:
        """Log a warning message"""
        self._log(LogLevel.WARNING, message, bold, icon)

    def error(self, message: str, bold: bool = False, icon: str | None = None) -> None:
        """Log an error message"""
        self._log(LogLevel.ERROR, message, bold, icon)

    def debug(self, message: str, bold: bool = False, icon: str | None = None) -> None:
        """Log a debug message (only shown if verbose is True)"""
        if self.verbose:
            self._log(LogLevel.DEBUG, message, bold, icon)

    def create_progress_spinner(self, message: str):
        """Create a Rich progress spinner that properly handles clearing"""
        progress = Progress(
            SpinnerColumn(spinner_name="dots12", style="accent"),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
            transient=True,
        )
        task_id = progress.add_task(message, total=None)
        return progress, task_id


# Create a global logger instance
logger = Logger()
