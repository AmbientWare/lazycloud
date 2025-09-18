from shared.models.statuses import KubernetesPhase


def get_status_color(status: KubernetesPhase) -> str:
    """Get color for status display."""
    if status == KubernetesPhase.RUNNING:
        return "green"
    elif status in (KubernetesPhase.PENDING, KubernetesPhase.PARTIALLY_RUNNING):
        return "yellow"
    elif status == KubernetesPhase.STOPPED:
        return "dim"
    else:
        return "red"
