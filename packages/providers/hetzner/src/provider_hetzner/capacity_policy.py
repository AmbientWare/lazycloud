from compute.providers import ProviderCapacityPolicy, ProviderOfferEligibility

HETZNER_CAPACITY_POLICY = ProviderCapacityPolicy(
    default_region="ash",
    allowed_regions=("ash",),
    allowed_offers=tuple(
        ProviderOfferEligibility(region="ash", instance_type=instance_type)
        for instance_type in ("ccx13", "ccx23", "ccx33", "ccx43", "ccx53", "ccx63")
    ),
    root_volume_gib=80,
)
