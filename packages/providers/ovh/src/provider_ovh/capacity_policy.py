from compute.providers import ProviderCapacityPolicy

OVH_CAPACITY_POLICY = ProviderCapacityPolicy(
    default_region="US-EAST-VA-1",
    allowed_regions=("US-EAST-VA-1", "US-WEST-OR-1"),
    allowed_instance_types=(
        "b3-16",
        "b3-32",
        "b3-64",
        "b3-128",
        "b3-256",
        "c3-8",
        "c3-16",
        "c3-32",
        "c3-64",
        "c3-128",
        "r3-32",
        "r3-64",
        "r3-128",
        "r3-256",
        "r3-512",
    ),
    root_volume_gib=80,
)
