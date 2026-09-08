from compute.providers import ProviderCapacityPolicy
from shared.gpu import GpuType

HYPERSTACK_CAPACITY_POLICY = ProviderCapacityPolicy(
    default_region="US-1",
    allowed_regions=("US-1",),
    allowed_instance_types=("n3-A100-SXM4x8", "n3-H100-SXM5x8"),
    root_volume_gib=100,
    warm_cpu_min=0,
)

GPU_MODELS = {
    "n3-A100-SXM4x8": GpuType.A100_80,
    "n3-H100-SXM5x8": GpuType.H100,
}
