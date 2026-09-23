from __future__ import annotations

from pydantic import Field, JsonValue
from shared.contracts import ContractModel

from worker.runtime_config import RuntimeBinaryConfig


class OciRuntimeContainerSpec(ContractModel):
    """One validated OCI bundle passed through the worker execution lifecycle."""

    container_id: str
    runtime: RuntimeBinaryConfig
    bundle_path: str
    config_path: str
    process_spec_dir: str
    sandbox_supervisor_token_path: str = ""
    spec: dict[str, JsonValue]
    docker_enabled: bool = False
    durable_root: bool = False
    """The root filesystem's upper layer is a durable disk; see RuntimeCommandRequest."""

    image_env: list[str] = Field(default_factory=list, repr=False)
