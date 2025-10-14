import importlib.metadata
from pathlib import Path

from lazycloud_cli.api import api
from lazycloud_cli.lazycloud_file import LazyCloudFile


def format_image_name(image: str) -> str:
    """Get the image name from the image string."""
    return image.split("/")[-1] if "/" in image else image


def get_current_deployment_name() -> str | None:
    """Get the deployment name from the current directory's lazycloud.yaml file.

    Returns:
        The deployment name if found, None otherwise.
    """
    try:
        lazycloud_file = LazyCloudFile.find_and_load(Path.cwd())
        if lazycloud_file:
            config = lazycloud_file.read()
            return config.deployment_name

    except Exception:
        pass

    return None


def validate_cli_version():
    # get required version from api
    required_version = api.versions.get_cli_version().version
    installed_version = importlib.metadata.version("lazycloud")

    if installed_version != required_version:
        # TODO: add auto upgrade here
        print("NEED TO ADD AUTO UPGRADE HERE")
