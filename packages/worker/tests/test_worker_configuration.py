from __future__ import annotations

import yaml
from pydantic import JsonValue, TypeAdapter
from worker.configuration import (
    WORKER_CONFIGURATION_SECTION,
    WorkerCapacityConfiguration,
    WorkerConfiguration,
    WorkerExecutionConfiguration,
    serialize_worker_configuration,
)

type JsonObject = dict[str, JsonValue]

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def test_agent_written_worker_yaml_section_parses_back_into_the_same_configuration() -> None:
    config = WorkerConfiguration(
        execution=WorkerExecutionConfiguration(
            capacity=WorkerCapacityConfiguration(
                cpu_millicores=2500,
                memory_mib=4096,
                gpu_type="L4",
                gpu_count=1,
            ),
            persistent=True,
        )
    )

    contents = serialize_worker_configuration(config)
    payload = _JSON_OBJECT.validate_python(yaml.safe_load(contents))
    effective = WorkerConfiguration.model_validate(payload[WORKER_CONFIGURATION_SECTION])

    assert effective == config
