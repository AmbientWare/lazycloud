from __future__ import annotations

from pydantic import JsonValue
from shared.contracts import ContractModel

from worker.image_lifecycle import ImageRuntimeConfig
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
    image_config: ImageRuntimeConfig
