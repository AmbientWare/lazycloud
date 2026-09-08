from compute.providers import ProviderCapacityPolicy

HETZNER_CAPACITY_POLICY = ProviderCapacityPolicy(
    default_region="ash",
    allowed_regions=("ash",),
    allowed_instance_types=("ccx13", "ccx23", "ccx33", "ccx43", "ccx53", "ccx63"),
    root_volume_gib=80,
    warm_cpu_min=1,
)
