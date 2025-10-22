from shared.models.statuses import KubernetesPhase


def get_status_class(status: KubernetesPhase) -> str:
    """Get CSS class for status display in widgets."""
    if status == KubernetesPhase.RUNNING:
        return "status-running"
    elif status in (KubernetesPhase.PENDING, KubernetesPhase.PARTIALLY_RUNNING):
        return "status-pending"
    elif status == KubernetesPhase.STOPPED:
        return "status-stopped"
    else:
        return "status-error"


def get_status_color(status: KubernetesPhase) -> str:
    """Get Rich color name for status in markup text"""
    if status == KubernetesPhase.RUNNING:
        return "green"
    elif status in (KubernetesPhase.PENDING, KubernetesPhase.PARTIALLY_RUNNING):
        return "yellow"
    elif status == KubernetesPhase.STOPPED:
        return "bright_black"  # Dimmed/muted
    else:
        return "red"
