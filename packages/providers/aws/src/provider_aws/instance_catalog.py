from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from types import MappingProxyType

from compute.providers import ProviderPurchaseLimit
from pydantic import BaseModel, ConfigDict, Field, model_validator
from shared.compute_policy import UnitName
from shared.gpu import SUPPORTED_GPU_TYPES, GpuType


class AwsInstanceCatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AwsInstanceCategory(StrEnum):
    Cpu = "cpu"
    NvidiaGpu = "nvidia_gpu"


class AwsInstanceCatalogEntry(AwsInstanceCatalogModel):
    instance_type: str
    kind: AwsInstanceCategory
    cpu_millicores: int = Field(gt=0)
    memory_mb: int = Field(gt=0)
    max_hourly_cost_micros: int = Field(gt=0)
    gpu: GpuType | None = None
    gpu_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_accelerator(self) -> AwsInstanceCatalogEntry:
        if self.kind is AwsInstanceCategory.Cpu and (self.gpu is not None or self.gpu_count):
            raise ValueError("CPU instances cannot declare GPU capacity")
        if self.kind is AwsInstanceCategory.NvidiaGpu and (self.gpu is None or self.gpu_count < 1):
            raise ValueError("NVIDIA GPU instances must declare GPU capacity")
        if self.gpu is not None and self.gpu not in SUPPORTED_GPU_TYPES:
            # Schedulable and priced are one list. A model offered here but absent
            # from SUPPORTED_GPU_TYPES would run and then bill nothing.
            raise ValueError(
                f"{self.instance_type} offers {self.gpu.value}, which the platform does not support"
            )
        return self


AWS_INSTANCE_CATALOG: tuple[AwsInstanceCatalogEntry, ...] = (
    AwsInstanceCatalogEntry(
        instance_type="m7i.2xlarge",
        max_hourly_cost_micros=430_423,
        kind=AwsInstanceCategory.Cpu,
        cpu_millicores=8_000,
        memory_mb=32 * 1024,
    ),
    AwsInstanceCatalogEntry(
        instance_type="m7i.4xlarge",
        max_hourly_cost_micros=833_623,
        kind=AwsInstanceCategory.Cpu,
        cpu_millicores=16_000,
        memory_mb=64 * 1024,
    ),
    AwsInstanceCatalogEntry(
        instance_type="m7i.8xlarge",
        max_hourly_cost_micros=1_640_023,
        kind=AwsInstanceCategory.Cpu,
        cpu_millicores=32_000,
        memory_mb=128 * 1024,
    ),
    AwsInstanceCatalogEntry(
        instance_type="m7i.12xlarge",
        max_hourly_cost_micros=2_446_423,
        kind=AwsInstanceCategory.Cpu,
        cpu_millicores=48_000,
        memory_mb=192 * 1024,
    ),
    AwsInstanceCatalogEntry(
        instance_type="m7i.16xlarge",
        max_hourly_cost_micros=3_252_823,
        kind=AwsInstanceCategory.Cpu,
        cpu_millicores=64_000,
        memory_mb=256 * 1024,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g4dn.xlarge",
        max_hourly_cost_micros=553_223,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=4_000,
        memory_mb=16 * 1024,
        gpu=GpuType.T4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g4dn.2xlarge",
        max_hourly_cost_micros=779_223,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=8_000,
        memory_mb=32 * 1024,
        gpu=GpuType.T4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g4dn.4xlarge",
        max_hourly_cost_micros=1_231_223,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=16_000,
        memory_mb=64 * 1024,
        gpu=GpuType.T4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g4dn.12xlarge",
        max_hourly_cost_micros=3_939_223,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=48_000,
        memory_mb=192 * 1024,
        gpu=GpuType.T4,
        gpu_count=4,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g4dn.metal",
        max_hourly_cost_micros=7_851_223,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=96_000,
        memory_mb=384 * 1024,
        gpu=GpuType.T4,
        gpu_count=8,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.xlarge",
        max_hourly_cost_micros=1_033_223,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=4_000,
        memory_mb=16 * 1024,
        gpu=GpuType.A10G,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.2xlarge",
        max_hourly_cost_micros=1_239_223,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=8_000,
        memory_mb=32 * 1024,
        gpu=GpuType.A10G,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.4xlarge",
        max_hourly_cost_micros=1_651_223,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=16_000,
        memory_mb=64 * 1024,
        gpu=GpuType.A10G,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.8xlarge",
        max_hourly_cost_micros=2_475_223,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=32_000,
        memory_mb=128 * 1024,
        gpu=GpuType.A10G,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.16xlarge",
        max_hourly_cost_micros=4_123_223,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=64_000,
        memory_mb=256 * 1024,
        gpu=GpuType.A10G,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.12xlarge",
        max_hourly_cost_micros=5_699_223,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=48_000,
        memory_mb=192 * 1024,
        gpu=GpuType.A10G,
        gpu_count=4,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.24xlarge",
        max_hourly_cost_micros=8_171_223,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=96_000,
        memory_mb=384 * 1024,
        gpu=GpuType.A10G,
        gpu_count=4,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.48xlarge",
        max_hourly_cost_micros=16_315_223,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=192_000,
        memory_mb=768 * 1024,
        gpu=GpuType.A10G,
        gpu_count=8,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.xlarge",
        max_hourly_cost_micros=832_023,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=4_000,
        memory_mb=16 * 1024,
        gpu=GpuType.L4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.2xlarge",
        max_hourly_cost_micros=1_004_823,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=8_000,
        memory_mb=32 * 1024,
        gpu=GpuType.L4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.4xlarge",
        max_hourly_cost_micros=1_350_423,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=16_000,
        memory_mb=64 * 1024,
        gpu=GpuType.L4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.8xlarge",
        max_hourly_cost_micros=2_041_623,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=32_000,
        memory_mb=128 * 1024,
        gpu=GpuType.L4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.16xlarge",
        max_hourly_cost_micros=3_424_023,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=64_000,
        memory_mb=256 * 1024,
        gpu=GpuType.L4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.12xlarge",
        max_hourly_cost_micros=4_628_823,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=48_000,
        memory_mb=192 * 1024,
        gpu=GpuType.L4,
        gpu_count=4,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.24xlarge",
        max_hourly_cost_micros=6_702_423,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=96_000,
        memory_mb=384 * 1024,
        gpu=GpuType.L4,
        gpu_count=4,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.48xlarge",
        max_hourly_cost_micros=13_377_623,
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=192_000,
        memory_mb=768 * 1024,
        gpu=GpuType.L4,
        gpu_count=8,
    ),
)

# Ceilings include 200 GiB gp3 and one public IPv4 address in us-east-1.
AWS_PURCHASE_LIMITS = tuple(
    ProviderPurchaseLimit(
        region="us-east-1",
        instance_type=instance.instance_type,
        max_hourly_cost_micros=instance.max_hourly_cost_micros,
    )
    for instance in AWS_INSTANCE_CATALOG
)

_INSTANCE_BY_TYPE = MappingProxyType(
    {instance.instance_type: instance for instance in AWS_INSTANCE_CATALOG}
)


def aws_instance_catalog_entry(instance_type: str) -> AwsInstanceCatalogEntry:
    try:
        return _INSTANCE_BY_TYPE[instance_type]
    except KeyError as exc:
        supported = ", ".join(sorted(_INSTANCE_BY_TYPE))
        raise ValueError(
            f"unsupported AWS managed-capacity instance type {instance_type!r}; "
            f"supported types: {supported}"
        ) from exc


def aws_managed_capacity_resource_name(
    kind: str,
    workspace_id: str,
    unit_name: UnitName,
    *,
    max_length: int,
) -> str:
    """Derive one AWS resource name for one provisioning unit.

    Keyed by the unit's name, not by the pool it feeds: several units share a
    pool, and two of them must not derive the same Auto Scaling group.
    """
    normalized_kind = _resource_part(kind) or "resource"
    normalized_unit = _resource_part(unit_name) or "unit"
    digest = hashlib.sha256(
        f"managed-capacity\0{kind}\0{workspace_id}\0{unit_name}".encode()
    ).hexdigest()[:10]
    prefix = f"cloud-pool-{normalized_kind}-"
    unit_limit = max_length - len(prefix) - len(digest) - 1
    if unit_limit < 1:
        raise ValueError("resource name maximum length is too small")
    trimmed_unit = normalized_unit[:unit_limit].rstrip("-") or "p"
    return f"{prefix}{trimmed_unit}-{digest}"


def aws_partition_for_region(region: str) -> str:
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    if region.startswith("cn-"):
        return "aws-cn"
    return "aws"


def aws_console_host(region: str) -> str:
    partition = aws_partition_for_region(region)
    if partition == "aws-us-gov":
        return "console.amazonaws-us-gov.com"
    if partition == "aws-cn":
        return "console.amazonaws.cn"
    return "console.aws.amazon.com"


def _resource_part(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower())
    return re.sub(r"-+", "-", normalized).strip("-")
