"""
LazyCloud Dashboard Theme - Lazygit/Lazydocker inspired.
"""

from typing import Tuple

from pydantic import BaseModel


class LazyCloudTheme(BaseModel):
    """LazyCloud Dashboard Theme Configuration - Modern terminal aesthetic."""

    # Primary colors (LazyCloud light blue)
    primary: str = "rgb(96,165,250)"
    primary_bright: str = "rgb(147,197,253)"

    # Base colors
    background: str = "transparent"
    surface: str = "transparent"

    # Text colors
    text: str = "rgb(229,231,235)"
    text_dim: str = "rgb(107,114,128)"
    text_accent: str = "rgb(96,165,250)"

    # Status colors
    success: str = "rgb(52,211,153)"
    warning: str = "rgb(251,191,36)"
    info: str = "rgb(147,197,253)"
    error: str = "rgb(248,113,113)"
    muted: str = "rgb(75,85,99)"

    # Border configuration
    border_color: str = "rgb(100,116,139)"
    border_color_focus: str = "rgb(96,165,250)"
    border_style: str = "round"
    
    # Layout
    padding: int = 1
    left_width: str = "25%"
    right_width: str = "75%"
    vertical_split: str = "50%"
    
    def get_border(self, focused: bool = False) -> Tuple[str, str]:
        """Get border style tuple (style, color)."""
        color = self.border_color_focus if focused else self.border_color
        return (self.border_style, color)
    
    def get_status_color(self, status: str) -> str:
        """Get appropriate color for a status string."""
        status_lower = status.lower()
        
        # Success states
        if any(word in status_lower for word in ["ready", "active", "healthy", "success", "running"]):
            return self.success
        # Warning states
        elif any(word in status_lower for word in ["pending", "waiting", "warning"]):
            return self.warning
        # Info states
        elif any(word in status_lower for word in ["deploying", "updating", "creating"]):
            return self.info
        # Error states
        elif any(word in status_lower for word in ["error", "failed", "unhealthy", "stopped"]):
            return self.error
        # Unknown
        else:
            return self.muted


# Default theme instance
theme = LazyCloudTheme()