from dataclasses import dataclass

from shared.container_requests import CONTAINER_MEMORY_BURST_FLOOR_MIB


@dataclass(frozen=True, slots=True)
class AwsComputeConfiguration:
    default_region: str = "us-east-1"
    default_instance_type: str = "m7i.2xlarge"
    initial_cpu_workers: int = 1
    min_cpu_workers: int = 1
    min_free_cpu_millicores: int = 1_000
    min_free_memory_mib: int = CONTAINER_MEMORY_BURST_FLOOR_MIB
    allowed_regions: tuple[str, ...] = ("us-east-1",)
    idle_timeout_seconds: int = 300
    root_volume_gib: int = 200


AWS_COMPUTE_CONFIGURATION = AwsComputeConfiguration()
