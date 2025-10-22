from typing import Tuple

from textual.theme import Theme

from lazycloud_cli.ui.colors import Colors


# Layout constants
class Layout:
    """Layout configuration constants."""

    padding = 1
    left_width = "25%"
    right_width = "75%"
    vertical_split = "50%"
    border_style = "round"


class Borders:
    """Reusable border style tuples."""

    default = (Layout.border_style, Colors.border)
    focus = (Layout.border_style, Colors.accent)
    success = (Layout.border_style, Colors.success)
    warning = (Layout.border_style, Colors.warning)
    error = (Layout.border_style, Colors.error)


# Neutral grey theme inspired by Nord
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


# Border helper for programmatic styling
def get_border(focused: bool = False) -> Tuple[str, str]:
    """Get border style tuple (style, color).

    Args:
        focused: If True, returns accent color. If False, returns default border.
    """
    return Borders.focus if focused else Borders.default
