from lazycloud_cli.ui.colors import Colors
from shared.models.statuses import KubernetesPhase


def get_status_color(status: KubernetesPhase) -> str:
    """Get color for status display using consistent Nord colors."""
    if status == KubernetesPhase.RUNNING:
        return Colors.Hex.success  # Nord green
    elif status in (KubernetesPhase.PENDING, KubernetesPhase.PARTIALLY_RUNNING):
        return Colors.Hex.warning  # Nord yellow
    elif status == KubernetesPhase.STOPPED:
        return Colors.Hex.text_dim  # Dimmed grey
    else:
        return Colors.Hex.error  # Nord red
