from textual.theme import Theme

from lazycloud_cli.ui.colors import Colors


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
