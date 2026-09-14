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


__all__ = ["DEFAULT_ENDPOINT_METHODS", "DeploymentKind", "StubKind"]
