"""Centralized theme configuration for consistent UI styling."""

from enum import StrEnum
from pydantic import BaseModel, Field


class Color(StrEnum):
    """Base colors available in the terminal."""

    # Basic colors
    BLACK = "black"
    RED = "red"
    GREEN = "green"
    YELLOW = "yellow"
    BLUE = "blue"
    MAGENTA = "magenta"
    CYAN = "cyan"
    WHITE = "white"

    # Bright variants
    BRIGHT_BLACK = "bright_black"
    BRIGHT_RED = "bright_red"
    BRIGHT_GREEN = "bright_green"
    BRIGHT_YELLOW = "bright_yellow"
    BRIGHT_BLUE = "bright_blue"
    BRIGHT_MAGENTA = "bright_magenta"
    BRIGHT_CYAN = "bright_cyan"
    BRIGHT_WHITE = "bright_white"

    # Special
    DEFAULT = "default"
    DIM = "dim"


class TextStyle(StrEnum):
    """Text style modifiers."""

    BOLD = "bold"
    DIM = "dim"
    ITALIC = "italic"
    UNDERLINE = "underline"
    BLINK = "blink"
    REVERSE = "reverse"
    STRIKE = "strike"


class SemanticColor(StrEnum):
    """Semantic color names for consistent usage."""

    PRIMARY = "primary"
    PRIMARY_BRIGHT = "primary_bright"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    INFO = "info"
    MUTED = "muted"
    HIGHLIGHT = "highlight"


class StyleContext(StrEnum):
    """Style contexts for semantic styling."""

    # Card contexts
    CARD_DEFAULT = "card.default"
    CARD_SUCCESS = "card.success"
    CARD_WARNING = "card.warning"
    CARD_ERROR = "card.error"
    CARD_INFO = "card.info"
    CARD_PRIMARY = "card.primary"

    # Table contexts
    TABLE_HEADER = "table.header"
    TABLE_PROPERTY = "table.property"
    TABLE_VALUE = "table.value"
    TABLE_MUTED = "table.muted"

    # Status contexts
    STATUS_SUCCESS = "status.success"
    STATUS_WARNING = "status.warning"
    STATUS_ERROR = "status.error"
    STATUS_PENDING = "status.pending"
    STATUS_RUNNING = "status.running"

    # Resource change contexts
    CHANGE_ADDED = "change.added"
    CHANGE_MODIFIED = "change.modified"
    CHANGE_REMOVED = "change.removed"

    # Message contexts
    MESSAGE_ERROR = "message.error"
    MESSAGE_WARNING = "message.warning"
    MESSAGE_SUCCESS = "message.success"
    MESSAGE_INFO = "message.info"

    # Diff contexts
    DIFF_OLD = "diff.old"
    DIFF_NEW = "diff.new"
    DIFF_CONTEXT = "diff.context"


class ColorScheme(BaseModel):
    """Color scheme for consistent theming across the application."""

    # Primary colors
    primary: str = Field(default=Color.CYAN, description="Main brand color")
    primary_bright: str = Field(
        default=Color.BRIGHT_CYAN, description="Highlighted primary"
    )

    # Status colors
    success: str = Field(default=Color.GREEN, description="Success state color")
    warning: str = Field(default=Color.YELLOW, description="Warning state color")
    error: str = Field(default=Color.RED, description="Error state color")
    info: str = Field(default=Color.BLUE, description="Info state color")

    # Semantic colors
    muted: str = Field(default=Color.DIM, description="Muted/secondary text")
    highlight: str = Field(default=Color.BRIGHT_WHITE, description="Highlighted text")

    # Component-specific colors
    table_header: str = Field(
        default=f"{TextStyle.BOLD} {Color.CYAN}", description="Table header style"
    )
    table_row_alt: str = Field(
        default=Color.DIM, description="Alternate table row style"
    )

    # Border styles for cards
    border_default: str = Field(default=Color.CYAN, description="Default card border")
    border_primary: str = Field(
        default=Color.BRIGHT_CYAN, description="Primary card border"
    )
    border_success: str = Field(default=Color.GREEN, description="Success card border")
    border_warning: str = Field(default=Color.YELLOW, description="Warning card border")
    border_error: str = Field(default=Color.RED, description="Error card border")
    border_info: str = Field(default=Color.BLUE, description="Info card border")

    # Text styles
    text_primary: str = Field(default=Color.DEFAULT, description="Primary text color")
    text_secondary: str = Field(default=Color.DIM, description="Secondary text color")
    text_emphasis: str = Field(
        default=TextStyle.BOLD, description="Emphasized text style"
    )

    # Specific use cases
    resource_added: str = Field(default=Color.GREEN, description="Added resource color")
    resource_modified: str = Field(
        default=Color.YELLOW, description="Modified resource color"
    )
    resource_removed: str = Field(
        default=Color.RED, description="Removed resource color"
    )

    # Status-specific colors
    status_pending: str = Field(
        default=Color.YELLOW, description="Pending status color"
    )
    status_running: str = Field(default=Color.BLUE, description="Running status color")
    status_ready: str = Field(default=Color.GREEN, description="Ready status color")
    status_failed: str = Field(default=Color.RED, description="Failed status color")
    status_unknown: str = Field(default=Color.DIM, description="Unknown status color")

    def get_color(self, semantic: SemanticColor) -> str:
        """Get color by semantic name."""
        mapping = {
            SemanticColor.PRIMARY: self.primary,
            SemanticColor.PRIMARY_BRIGHT: self.primary_bright,
            SemanticColor.SUCCESS: self.success,
            SemanticColor.WARNING: self.warning,
            SemanticColor.ERROR: self.error,
            SemanticColor.INFO: self.info,
            SemanticColor.MUTED: self.muted,
            SemanticColor.HIGHLIGHT: self.highlight,
        }
        return mapping.get(semantic, self.text_primary)


# Default theme instance
theme = ColorScheme()


def combine_styles(*styles: str) -> str:
    """Combine multiple style strings into one."""
    return " ".join(filter(None, styles))
