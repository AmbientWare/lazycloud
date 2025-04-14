# rich logging
from enum import Enum
from typing import List, Dict, Any
from rich.console import Console
from rich.theme import Theme
from rich import print as rprint
from rich.table import Table
from rich.box import ROUNDED
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.live import Live
from rich.spinner import Spinner as RichSpinner


REMOVABLE_TABLE_NAMES = ["id", "created_at", "updated_at", "user_id", "machine_uuid"]


# Define a custom theme for consistent styling
custom_theme = Theme(
    {
        "info": "white",
        "success": "green",
        "status": "yellow",
        "warning": "yellow",
        "error": "red",
        "debug": "blue",
    }
)


class LogLevel(Enum):
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

    def _log(self, level: LogLevel, message: str, bold: bool = False) -> None:
        """Internal logging method that handles styling"""
        style = level.value
        if bold:
            style = f"bold {style}"

        # Use error console for errors
        if level == LogLevel.ERROR:
            self.error_console.print(message, style=style)
        else:
            self.console.print(message, style=style)

    def info(self, message: str, bold: bool = False) -> None:
        """Log an info message"""
        self._log(LogLevel.INFO, message, bold)

    def success(self, message: str, bold: bool = False) -> None:
        """Log a success message"""
        self._log(LogLevel.SUCCESS, message, bold)

    def status(self, message: str, bold: bool = False) -> None:
        """Log a status message"""
        self._log(LogLevel.STATUS, message, bold)

    def warning(self, message: str, bold: bool = False) -> None:
        """Log a warning message"""
        self._log(LogLevel.WARNING, message, bold)

    def error(self, message: str, bold: bool = False) -> None:
        """Log an error message"""
        self._log(LogLevel.ERROR, message, bold)

    def debug(self, message: str, bold: bool = False) -> None:
        """Log a debug message (only shown if verbose is True)"""
        if self.verbose:
            self._log(LogLevel.DEBUG, message, bold)

    def table(self, data: List[Dict[str, Any]]) -> None:
        """Log a table with enhanced styling

        Args:
            data: List of dictionaries to display as a table
            title: Optional title for the table
        """
        if not data:
            self.info("No data to display in table")
            return

        # Create table with columns and styling
        table = Table(
            show_header=True,
            header_style="bold cyan",
            title_style="bold magenta",
            title_justify="center",
            border_style="blue",
            box=ROUNDED,
            expand=False,
            width=None,
            show_lines=True,
        )

        # Get all unique keys and calculate max length for each column in a single pass
        columns = {}
        for item in data:
            for key, value in item.items():
                if key not in REMOVABLE_TABLE_NAMES:
                    if key not in columns:
                        columns[key] = 0
                    # Calculate max length for this column
                    value_str = str(value)
                    columns[key] = max(columns[key], len(value_str))

        # Add columns with styling
        for column, max_length in columns.items():
            # For columns that might contain long text like SSH keys, use truncation
            if max_length > 50:
                table.add_column(column, style="cyan", no_wrap=True, max_width=50)
            else:
                table.add_column(column, style="cyan", no_wrap=True)

        # Add rows
        for item in data:
            row = [str(item.get(column, "")) for column in columns]
            table.add_row(*row, style="white")

        # print line above and below the table
        self.console.print(table)

    def print(self, *args, **kwargs):
        """Direct access to Rich's print function"""
        rprint(*args, **kwargs)

    def create_spinner(self, message: str, style: str = "dots"):
        """Create a Rich spinner with a live display"""
        spinner = RichSpinner(style, text=message)
        return Live(spinner, console=self.console, refresh_per_second=10)

    def create_progress_spinner(self, message: str):
        """Create a Rich progress spinner that properly handles clearing"""
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
            transient=True,  # This ensures the progress bar is removed when done
        )
        task_id = progress.add_task(message, total=None)
        return progress, task_id


# Create a global logger instance
logger = Logger()
