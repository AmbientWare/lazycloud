from lazycloud_cli.ui.colors import Colors
from shared.models.statuses import KubernetesPhase


def get_status_class(status: KubernetesPhase) -> str:
    """Get CSS class for status display in widgets."""
    if status == KubernetesPhase.RUNNING:
        return "status-running"
    elif status in (KubernetesPhase.PENDING, KubernetesPhase.PARTIALLY_RUNNING, KubernetesPhase.TERMINATING):
        return "status-pending"
    elif status == KubernetesPhase.STOPPED:
        return "status-stopped"
    else:
        return "status-error"


def get_status_color(status: KubernetesPhase) -> str:
    """Get Rich color hex for status in markup text (matches CSS theme)."""
    if status == KubernetesPhase.RUNNING:
        return Colors.Hex.success
    elif status in (KubernetesPhase.PENDING, KubernetesPhase.PARTIALLY_RUNNING, KubernetesPhase.TERMINATING):
        return Colors.Hex.warning
    elif status == KubernetesPhase.STOPPED:
        return f"{Colors.Hex.primary} 50%"
    else:
        return Colors.Hex.error


def get_status_color_from_string(status: str) -> str:
    """Get Rich color hex from string status (for volumes/networks)."""
    status_lower = status.lower()
    if status_lower in ("running", "ready", "active"):
        return Colors.Hex.success
    elif status_lower in ("pending", "waiting", "terminating"):
        return Colors.Hex.warning
    elif status_lower in ("error", "failed"):
        return Colors.Hex.error
    elif status_lower == "stopped":
        return f"{Colors.Hex.primary} 50%"
    else:
        return Colors.Hex.text
