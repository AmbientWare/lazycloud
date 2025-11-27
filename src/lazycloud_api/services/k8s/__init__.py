import pathlib

from pydantic import BaseModel


class Charts(BaseModel):
    compose: pathlib.Path
    namespace: pathlib.Path


def get_chart_paths() -> Charts:
    # get current directory based on this file
    current_dir = pathlib.Path(__file__).parent
    chart_directory = current_dir / "charts"

    compose_chart = chart_directory / "lazycloud-compose"
    namespace_chart = chart_directory / "lazycloud-namespace"

    return Charts(compose=compose_chart, namespace=namespace_chart)


def create_ns_name(workspace_id: str) -> str:
    namespace = f"lc-{workspace_id}".lower()

    return namespace


def create_release_name(workspace_id: str, deployment_name: str) -> str:
    """Create a Helm release name (max 53 chars for DNS-1123 compliance)."""
    # Use first 8 chars of workspace ID for uniqueness + deployment name
    # Format: lc-{ws_prefix}-{deployment_name} (max 53 chars)
    ws_prefix = workspace_id.replace("-", "")[:8]
    max_deploy_len = 53 - len(f"lc-{ws_prefix}-")
    deploy_part = deployment_name[:max_deploy_len].rstrip("-")
    return f"lc-{ws_prefix}-{deploy_part}".lower()
