class Colors:
    # Hex colors - for Textual dashboard
    class Hex:
        accent = "#7fc4cf"
        text = "#d8dee9"
        text_muted = "#4c566a"
        text_dim = "#3b4252"

        background = "#000000"
        surface = "#1a1a1a"
        border = "#4c566a"

        primary = "#ffffff"
        secondary = "#7fc4cf"
        success = "#5cd85c"
        warning = "#f7d66e"
        error = "#ff6666"
        info = "#7fc4cf"

    # ANSI names - for Rich CLI
    class Ansi:
        accent = "cyan"
        text = "default"
        text_muted = "bright_black"
        text_dim = "black"

        background = "default"
        surface = "default"
        border = "bright_black"

        primary = "default"
        secondary = "cyan"
        success = "green"
        warning = "yellow"
        error = "red"
        info = "bright_blue"

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
