# click logging
import click
from enum import Enum


class LogLevel(Enum):
    """Log levels with their corresponding colors and styles"""

    INFO = ("white", None)
    SUCCESS = ("green", None)
    WARNING = ("yellow", None)
    ERROR = ("red", None)
    DEBUG = ("blue", None)


class Logger:
    """A logger that uses Click's color support for terminal output"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def _log(self, level: LogLevel, message: str, bold: bool = False) -> None:
        """Internal logging method that handles color and style"""
        color, style = level.value
        click.echo(
            click.style(message, fg=color, bold=bold, italic=style == "italic"),
            err=(level == LogLevel.ERROR),
        )

    def info(self, message: str, bold: bool = False) -> None:
        """Log an info message"""
        self._log(LogLevel.INFO, message, bold)

    def success(self, message: str, bold: bool = False) -> None:
        """Log a success message"""
        self._log(LogLevel.SUCCESS, message, bold)

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


# Create a global logger instance
logger = Logger()
