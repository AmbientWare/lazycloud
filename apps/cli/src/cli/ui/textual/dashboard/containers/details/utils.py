from models.statuses import KubernetesPhase

from cli.ui.colors import Colors


def get_status_class(status: KubernetesPhase) -> str:
    """Get CSS class for status display in widgets."""
    if status == KubernetesPhase.RUNNING:
        return "status-running"
    elif status in (
        KubernetesPhase.PENDING,
        KubernetesPhase.STARTING,
        KubernetesPhase.UPDATING,
        KubernetesPhase.PARTIALLY_RUNNING,
        KubernetesPhase.TERMINATING,
    ):
        return "status-pending"
    elif status == KubernetesPhase.STOPPED:
        return "status-stopped"
    else:
        return "status-error"


def get_status_color(status: KubernetesPhase) -> str:
    """Get Rich color hex for status in markup text (matches CSS theme)."""
    if status == KubernetesPhase.RUNNING:
        return Colors.Hex.success
    elif status in (
        KubernetesPhase.PENDING,
        KubernetesPhase.STARTING,
        KubernetesPhase.UPDATING,
        KubernetesPhase.PARTIALLY_RUNNING,
        KubernetesPhase.TERMINATING,
    ):
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
    elif status_lower in ("pending", "waiting", "terminating", "updating", "starting", "stopping", "restarting"):
        return Colors.Hex.warning
    elif status_lower in ("error", "failed"):
        return Colors.Hex.error
    elif status_lower in ("stopped", "exited"):
        return f"{Colors.Hex.primary} 50%"
    else:
        return Colors.Hex.text
