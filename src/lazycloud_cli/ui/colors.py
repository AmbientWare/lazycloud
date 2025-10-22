class Colors:
    # Hex colors - for Textual dashboard
    class Hex:
        accent = "#8dd4e0"
        text = "#d8dee9"
        text_muted = "#4c566a"
        text_dim = "#3b4252"

        background = "#2e3440"
        surface = "#3b4252"
        border = "#4c566a"

        primary = "#8dd4e0"
        secondary = "#6a8dc8"
        success = "#a8d088"
        warning = "#f0d478"
        error = "#d85060"
        info = "#6a8dc8"

    # ANSI names - for Rich CLI
    class Ansi:
        accent = "cyan"
        text = "default"
        text_muted = "bright_black"
        text_dim = "black"

        background = "default"
        surface = "default"
        border = "bright_black"

        primary = "cyan"
        secondary = "blue"
        success = "green"
        warning = "yellow"
        error = "red"
        info = "blue"

    accent = Hex.accent
    text = Hex.text
    text_muted = Hex.text_muted
    background = Hex.background
    surface = Hex.surface
    border = Hex.border
    primary = Hex.primary
    secondary = Hex.secondary
    success = Hex.success
    warning = Hex.warning
    error = Hex.error
    info = Hex.info
