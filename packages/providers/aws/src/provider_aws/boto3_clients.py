from __future__ import annotations

from typing import Protocol, TypeGuard


class Boto3ClientFactory(Protocol):
    def client(self, service_name: str) -> object: ...


def is_boto3_client_factory(value: object) -> TypeGuard[Boto3ClientFactory]:
    return callable(getattr(value, "client", None))


def has_operations(value: object, operations: tuple[str, ...]) -> bool:
    """Report whether a boto3 client exposes every API operation a caller needs.

    boto3 builds its clients at runtime from service models, so the operations a
    client carries are only knowable by asking the instance for them.
    """

    return all(callable(getattr(value, operation, None)) for operation in operations)
