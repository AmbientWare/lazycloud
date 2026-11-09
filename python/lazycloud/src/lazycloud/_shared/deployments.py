from lazycloud._shared.enums import StringEnum

DEFAULT_ENDPOINT_METHODS = ("GET", "POST")


class DeploymentKind(StringEnum):
    Function = "function"
    Endpoint = "endpoint"
    Asgi = "asgi"
    Pod = "pod"
    Sandbox = "sandbox"


__all__ = [
    "DEFAULT_ENDPOINT_METHODS",
    "DeploymentKind",
]
