from infrastructure.constructs.apps import AppConfig


def get_apps(namespace_name: str = "lazycloud-prod") -> list[AppConfig]:
    """Get lazycloud application configurations for prod environment"""

    # Common configuration for all apps
    apps = []

    return apps
