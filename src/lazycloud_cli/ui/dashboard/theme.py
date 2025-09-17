"""
LazyCloud Dashboard Theme - Rich/Typer inspired.
"""

from typing import Tuple

from pydantic import BaseModel


class LazyCloudTheme(BaseModel):
    """LazyCloud Dashboard Theme Configuration - Rich/Typer inspired colors."""
    
    # Primary colors (Rich blue theme)
    primary: str = "rgb(95,135,255)"  # Rich's primary blue
    primary_bright: str = "rgb(130,170,255)"  # Lighter blue for focus
    
    # Base colors
    background: str = "transparent"
    surface: str = "transparent"
    
    # Text colors
    text: str = "rgb(248,248,242)"  # Rich's default text (off-white)
    text_dim: str = "rgb(98,114,164)"  # Rich's dim text (muted blue-gray)
    text_accent: str = "rgb(139,233,253)"  # Rich's cyan accent
    
    # Status colors (Rich's semantic colors)
    success: str = "rgb(80,250,123)"  # Rich's green
    warning: str = "rgb(241,250,140)"  # Rich's yellow
    info: str = "rgb(189,147,249)"  # Rich's purple/magenta
    error: str = "rgb(255,85,85)"  # Rich's red
    muted: str = "rgb(68,71,90)"  # Rich's comment gray
    
    # Border configuration (Rich's panel style)
    border_color: str = "rgb(98,114,164)"  # Muted blue-gray
    border_color_focus: str = "rgb(139,233,253)"  # Cyan for focus
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