from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shared.compute_policy import UnitName


class AwsInstanceCatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AwsInstanceCategory(StrEnum):
    Cpu = "cpu"
    NvidiaGpu = "nvidia_gpu"


class AwsGpuModel(StrEnum):
    """Cards EC2 rents, named as `shared.gpu.GpuType` names them.

    The two vocabularies must agree: a workload asks in `GpuType` terms and the
    scheduler compares the answer against what a worker reports, so a name that
    differs here is a type nobody can ever be scheduled onto.
    """

    T4 = "T4"
    A10G = "A10G"
    L4 = "L4"
    L40S = "L40S"
    A100_40 = "A100-40"
    A100_80 = "A100-80"
    H100 = "H100"
    H200 = "H200"
    B200 = "B200"


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
        instance_type="g4dn.xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=4_000,
        memory_mb=16 * 1024,
        gpu=AwsGpuModel.T4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g4dn.2xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=8_000,
        memory_mb=32 * 1024,
        gpu=AwsGpuModel.T4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g4dn.4xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=16_000,
        memory_mb=64 * 1024,
        gpu=AwsGpuModel.T4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g4dn.8xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=32_000,
        memory_mb=128 * 1024,
        gpu=AwsGpuModel.T4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g4dn.16xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=64_000,
        memory_mb=256 * 1024,
        gpu=AwsGpuModel.T4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g4dn.12xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=48_000,
        memory_mb=192 * 1024,
        gpu=AwsGpuModel.T4,
        gpu_count=4,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g4dn.metal",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=96_000,
        memory_mb=384 * 1024,
        gpu=AwsGpuModel.T4,
        gpu_count=8,
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
        instance_type="g5.4xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=16_000,
        memory_mb=64 * 1024,
        gpu=AwsGpuModel.A10G,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.8xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=32_000,
        memory_mb=128 * 1024,
        gpu=AwsGpuModel.A10G,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.16xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=64_000,
        memory_mb=256 * 1024,
        gpu=AwsGpuModel.A10G,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.12xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=48_000,
        memory_mb=192 * 1024,
        gpu=AwsGpuModel.A10G,
        gpu_count=4,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.24xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=96_000,
        memory_mb=384 * 1024,
        gpu=AwsGpuModel.A10G,
        gpu_count=4,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g5.48xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=192_000,
        memory_mb=768 * 1024,
        gpu=AwsGpuModel.A10G,
        gpu_count=8,
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
    AwsInstanceCatalogEntry(
        instance_type="g6.4xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=16_000,
        memory_mb=64 * 1024,
        gpu=AwsGpuModel.L4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.8xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=32_000,
        memory_mb=128 * 1024,
        gpu=AwsGpuModel.L4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.16xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=64_000,
        memory_mb=256 * 1024,
        gpu=AwsGpuModel.L4,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.12xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=48_000,
        memory_mb=192 * 1024,
        gpu=AwsGpuModel.L4,
        gpu_count=4,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.24xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=96_000,
        memory_mb=384 * 1024,
        gpu=AwsGpuModel.L4,
        gpu_count=4,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6.48xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=192_000,
        memory_mb=768 * 1024,
        gpu=AwsGpuModel.L4,
        gpu_count=8,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6e.xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=4_000,
        memory_mb=32 * 1024,
        gpu=AwsGpuModel.L40S,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6e.2xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=8_000,
        memory_mb=64 * 1024,
        gpu=AwsGpuModel.L40S,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6e.4xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=16_000,
        memory_mb=128 * 1024,
        gpu=AwsGpuModel.L40S,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6e.8xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=32_000,
        memory_mb=256 * 1024,
        gpu=AwsGpuModel.L40S,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6e.16xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=64_000,
        memory_mb=512 * 1024,
        gpu=AwsGpuModel.L40S,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6e.12xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=48_000,
        memory_mb=384 * 1024,
        gpu=AwsGpuModel.L40S,
        gpu_count=4,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6e.24xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=96_000,
        memory_mb=768 * 1024,
        gpu=AwsGpuModel.L40S,
        gpu_count=4,
    ),
    AwsInstanceCatalogEntry(
        instance_type="g6e.48xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=192_000,
        memory_mb=1536 * 1024,
        gpu=AwsGpuModel.L40S,
        gpu_count=8,
    ),
    AwsInstanceCatalogEntry(
        instance_type="p4d.24xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=96_000,
        memory_mb=1152 * 1024,
        gpu=AwsGpuModel.A100_40,
        gpu_count=8,
    ),
    AwsInstanceCatalogEntry(
        instance_type="p4de.24xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=96_000,
        memory_mb=1152 * 1024,
        gpu=AwsGpuModel.A100_80,
        gpu_count=8,
    ),
    AwsInstanceCatalogEntry(
        instance_type="p5.4xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=16_000,
        memory_mb=256 * 1024,
        gpu=AwsGpuModel.H100,
        gpu_count=1,
    ),
    AwsInstanceCatalogEntry(
        instance_type="p5.48xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=192_000,
        memory_mb=2048 * 1024,
        gpu=AwsGpuModel.H100,
        gpu_count=8,
    ),
    AwsInstanceCatalogEntry(
        instance_type="p5e.48xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=192_000,
        memory_mb=2048 * 1024,
        gpu=AwsGpuModel.H200,
        gpu_count=8,
    ),
    AwsInstanceCatalogEntry(
        instance_type="p5en.48xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=192_000,
        memory_mb=2048 * 1024,
        gpu=AwsGpuModel.H200,
        gpu_count=8,
    ),
    AwsInstanceCatalogEntry(
        instance_type="p6-b200.48xlarge",
        kind=AwsInstanceCategory.NvidiaGpu,
        cpu_millicores=192_000,
        memory_mb=2048 * 1024,
        gpu=AwsGpuModel.B200,
        gpu_count=8,
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
