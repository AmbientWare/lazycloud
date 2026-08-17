from __future__ import annotations

from shared.contracts import ContractModel
from shared.enums import StringEnum


class StubScopedContainerPrefix(StringEnum):
    Sandbox = "sandbox"
    Pod = "pod"
    Endpoint = "endpoint"


class StubScopedContainerIdParts(ContractModel):
    prefix: StubScopedContainerPrefix
    stub_id: str
    suffix: str


def extract_stub_id_from_container_id(container_id: str) -> str:
    parts = container_id.split("-")
    if len(parts) < 7:
        msg = "invalid container id"
        raise ValueError(msg)
    return "-".join(parts[1:6])


def parse_stub_scoped_container_id(container_id: str) -> StubScopedContainerIdParts | None:
    prefix_text, separator, _ = container_id.partition("-")
    if not separator:
        return None
    try:
        prefix = StubScopedContainerPrefix(prefix_text)
        stub_id = extract_stub_id_from_container_id(container_id)
    except ValueError:
        return None

    parts = container_id.split("-")
    return StubScopedContainerIdParts(
        prefix=prefix,
        stub_id=stub_id,
        suffix="-".join(parts[6:]),
    )


def extract_stub_id_from_stub_scoped_container_id(container_id: str) -> str:
    parsed = parse_stub_scoped_container_id(container_id)
    return parsed.stub_id if parsed is not None else ""
