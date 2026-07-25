from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AwsInstanceCatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AwsInstanceCategory(StrEnum):
    Cpu = "cpu"
    NvidiaGpu = "nvidia_gpu"


class AwsGpuModel(StrEnum):
    A10G = "A10G"
    L4 = "L4"


class AwsInstanceCatalogEntry(AwsInstanceCatalogModel):
    instance_type: str
    kind: AwsInstanceCategory
    cpu_millicores: int = Field(gt=0)
    memory_mb: int = Field(gt=0)
    gpu: AwsGpuModel | None = None
    gpu_count: int = Field(default=0, ge=0)
    local_nvme: bool = True

    @model_validator(mode="after")
    def validate_accelerator(self) -> AwsInstanceCatalogEntry:
        if self.kind is AwsInstanceCategory.Cpu and (self.gpu is not None or self.gpu_count):
            raise ValueError("CPU instances cannot declare GPU capacity")
        if self.kind is AwsInstanceCategory.NvidiaGpu and (self.gpu is None or self.gpu_count < 1):
            raise ValueError("NVIDIA GPU instances must declare GPU capacity")
        return self


AWS_INSTANCE_CATALOG: tuple[AwsInstanceCatalogEntry, ...] = (
    AwsInstanceCatalogEntry(
        instance_type="i4i.large",
        kind=AwsInstanceCategory.Cpu,
        cpu_millicores=2_000,
        memory_mb=16 * 1024,
    ),
    AwsInstanceCatalogEntry(
        instance_type="i4i.xlarge",
        kind=AwsInstanceCategory.Cpu,
        cpu_millicores=4_000,
        memory_mb=32 * 1024,
    ),
    AwsInstanceCatalogEntry(
        instance_type="i4i.2xlarge",
        kind=AwsInstanceCategory.Cpu,
        cpu_millicores=8_000,
        memory_mb=64 * 1024,
    ),
    AwsInstanceCatalogEntry(
        instance_type="i4i.4xlarge",
        kind=AwsInstanceCategory.Cpu,
        cpu_millicores=16_000,
        memory_mb=128 * 1024,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=4_000,
        memory_mb=16 * 1024,
        gpu=AwsGpuModel.A10G,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.2xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=8_000,
        memory_mb=32 * 1024,
        gpu=AwsGpuModel.A10G,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=4_000,
        memory_mb=16 * 1024,
        gpu=AwsGpuModel.L4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.2xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=8_000,
        memory_mb=32 * 1024,
        gpu=AwsGpuModel.L4,
        gpu_count=1,
    ),
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
    pool_name: str,
    *,
    max_length: int,
) -> str:
    normalized_kind = _resource_part(kind) or "resource"
    normalized_pool = _resource_part(pool_name) or "pool"
    digest = hashlib.sha256(
        f"managed-capacity\0{kind}\0{workspace_id}\0{pool_name}".encode()
    ).hexdigest()[:10]
    prefix = f"cloud-pool-{normalized_kind}-"
    pool_limit = max_length - len(prefix) - len(digest) - 1
    if pool_limit < 1:
        raise ValueError("resource name maximum length is too small")
    trimmed_pool = normalized_pool[:pool_limit].rstrip("-") or "p"
    return f"{prefix}{trimmed_pool}-{digest}"


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
