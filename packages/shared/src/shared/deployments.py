from shared.enums import StringEnum

DEFAULT_ENDPOINT_METHODS = ("GET", "POST")


class DeploymentKind(StringEnum):
    Function = "function"
    Endpoint = "endpoint"
    Asgi = "asgi"
    Pod = "pod"
    Sandbox = "sandbox"
    Command = "command"


class StubKind(StringEnum):
    Function = "function"
    Endpoint = "endpoint"
    Asgi = "asgi"
    Pod = "pod"
    Shell = "shell"
    Sandbox = "sandbox"
    Command = "command"


class PodRole(StringEnum):
    """What a pod is for, which decides the defaults it resolves to."""

    Service = "service"
    """A long-running container serving its ports or its command."""

    Devbox = "devbox"
    """A dev machine reached over SSH, whose root filesystem is a durable disk."""


class DevboxState(StringEnum):
    Running = "running"
    Starting = "starting"
    """A container is placed or booting; an SSH connection waits for it."""

    Stopped = "stopped"
    """No container; the next SSH connection starts one from the disk."""


__all__ = ["DEFAULT_ENDPOINT_METHODS", "DeploymentKind", "DevboxState", "PodRole", "StubKind"]
