import importlib.metadata
from pathlib import Path

from lazycloud_cli.api import api
from lazycloud_cli.lazycloud_file import LazyCloudFile


def format_image_name(image: str) -> str:
    """Get the image name from the image string."""
    return image.split("/")[-1] if "/" in image else image


def format_cpu(cpu_str: str) -> str:
    """
    Format CPU value to Docker Compose style (decimal cores)
    """
    if not cpu_str or cpu_str == "N/A":
        return "N/A"

    try:
        # Handle millicores (e.g., "100m")
        if cpu_str.endswith("m"):
            millicores = float(cpu_str.rstrip("m"))
            cores = millicores / 1000
            # Format: show 2 decimals for small values, 1 for larger
            if cores < 0.1:
                return f"{cores:.3f}"
            elif cores < 1:
                return f"{cores:.2f}"
            else:
                return f"{cores:.1f}"

        else:
            # Already in cores (e.g., "0.5", "2")
            cores = float(cpu_str)
            if cores < 1:
                return f"{cores:.2f}"
            else:
                return f"{cores:.1f}"

    except (ValueError, AttributeError):
        return cpu_str


def format_memory(memory_str: str) -> str:
    """Format memory value to Docker Compose style (human-readable)"""
    if not memory_str or memory_str == "N/A":
        return "N/A"

    try:
        # Parse Kubernetes memory units
        if memory_str.endswith("Mi"):
            mib = float(memory_str.rstrip("Mi"))
            # Convert to GiB if >= 1024 MiB
            if mib >= 1024:
                gib = mib / 1024
                return f"{gib:.1f}G" if gib < 10 else f"{gib:.0f}G"
            else:
                return f"{mib:.0f}M"

        elif memory_str.endswith("Gi"):
            gib = float(memory_str.rstrip("Gi"))
            return f"{gib:.1f}G" if gib < 10 else f"{gib:.0f}G"

        elif memory_str.endswith("Ki"):
            kib = float(memory_str.rstrip("Ki"))
            mib = kib / 1024
            if mib >= 1024:
                gib = mib / 1024
                return f"{gib:.1f}G"
            else:
                return f"{mib:.0f}M"

        elif memory_str.endswith("M"):
            # Already in MB
            mb = float(memory_str.rstrip("M"))
            if mb >= 1000:
                gb = mb / 1000
                return f"{gb:.1f}G"
            else:
                return f"{mb:.0f}M"

        elif memory_str.endswith("G"):
            # Already in GB
            return memory_str

        else:
            # Try to parse as raw bytes
            bytes_val = float(memory_str)
            mib = bytes_val / (1024 * 1024)
            if mib >= 1024:
                gib = mib / 1024
                return f"{gib:.1f}G"
            else:
                return f"{mib:.0f}M"

    except (ValueError, AttributeError):
        return memory_str


def get_current_deployment_name() -> str | None:
    """Get the deployment name from the current directory's lazycloud.yaml file"""
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
