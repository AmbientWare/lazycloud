from __future__ import annotations

import pytest
from provider_aws import (
    AWS_INSTANCE_CATALOG,
    AwsInstanceCategory,
    aws_instance_catalog_entry,
)
from shared.gpu import GpuType, normalize_gpu_type


def test_instance_catalog_rejects_unsupported_capacity() -> None:
    with pytest.raises(ValueError, match="unsupported AWS managed-capacity instance type"):
        aws_instance_catalog_entry("mystery.large")


def test_every_offered_gpu_is_a_name_the_scheduler_can_match() -> None:
    """The catalog and the request vocabulary have to be the same words.

    A workload names a GPU in `GpuType` terms; the scheduler compares that against
    what the worker on the instance reports, which comes from this catalog. A name
    here that normalises to something else, or that `GpuType` does not define, is
    an instance type that provisions and then never receives work — the failure
    arrives as a retry limit long after the machine is billing.
    """
    offered = {
        entry.gpu.value
        for entry in AWS_INSTANCE_CATALOG
        if entry.kind is AwsInstanceCategory.NvidiaGpu and entry.gpu is not None
    }

    assert offered
    assert offered <= {gpu_type.value for gpu_type in GpuType}
    assert all(normalize_gpu_type(name) == name for name in offered)
