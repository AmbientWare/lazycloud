from enum import StrEnum

from textual.theme import Theme

from cli.ui.colors import Colors


class Icons(StrEnum):
    """Icons configuration constants."""

    ROCKET = "🚀"
    WRENCH = "🔧"
    LOCK_KEY = "🔐"
    FILE = "📄"
    COMPUTER = "🖥️ "  # space for formatting
    SETTINGS = "⚙️ "  # space for formatting
    TREND = "📈"  # space for formatting
    OVERVIEW = "📊"
    SAVE = "💾"
    NETWORKS = "🌐"
    RESTART = "🔄"
    HEALTH = "❤️ "  # space for formatting
    WARNING = "⚠️"
    CHECKMARK = "✔️"
    TRASH = "🗑️ "  # space for formatting


class Symbols(StrEnum):
    """Symbols configuration constants."""

    BULLET = "•"
    TARGET = "⦿"
    WARNING = "⚠"
    CHECKMARK = "✔"
    RIGHT_TRIANGLE = "▶"
    CIRCLE_FILLED = "●"
    CIRCLE_EMPTY = "○"
    CIRCLE_HALF = "◐"
    CROSS = "✗"


class Layout:
    """Layout configuration constants."""

    padding = 1
    left_width = "25%"
    right_width = "75%"
    vertical_split = "50%"
    border_style = "round"


lazycloud_theme = Theme(
    name="lazycloud",
    primary=Colors.border,
    secondary=Colors.secondary,
    accent=Colors.accent,
    foreground=Colors.text,
    success=Colors.success,
    warning=Colors.warning,
    error=Colors.error,
    surface=Colors.surface,
    panel=Colors.surface,
    dark=True,
    variables={
        "border": Colors.border,
        "border-blurred": Colors.text_muted,
        "block-cursor-text-style": "none",
        "block-cursor-background": Colors.secondary,
        "footer-key-foreground": Colors.accent,
        "input-selection-background": f"{Colors.secondary} 35%",
    },
)
