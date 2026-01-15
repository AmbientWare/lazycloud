from pydantic import BaseModel


class BuildArg(BaseModel):
    """A single build argument."""

    key: str
    value: str | None = None  # None means needs to be collected from user


class ServiceBuildArgs(BaseModel):
    """Build arguments for a specific service."""

    service_name: str
    args: list[BuildArg]


class BuildArgsCollection(BaseModel):
    """Collection of build arguments for all services."""

    services: list[ServiceBuildArgs]

    def has_args_to_collect(self) -> bool:
        """Check if there are any build args that need values."""
        for service in self.services:
            for arg in service.args:
                if arg.value is None:
                    return True
        return False

    def get_args_for_service(self, service_name: str) -> dict[str, str]:
        """Get build args as a dict for a specific service."""
        for service in self.services:
            if service.service_name == service_name:
                return {arg.key: arg.value or "" for arg in service.args}
        return {}
