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
    return f"lc-{workspace_id}-{deployment_name}".lower()
