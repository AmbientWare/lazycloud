from compute.providers import ProviderCapacityPolicy, ProviderPurchaseLimit

HETZNER_CAPACITY_POLICY = ProviderCapacityPolicy(
    default_region="ash",
    allowed_regions=("ash",),
    purchase_limits=(
        ProviderPurchaseLimit(region="ash", instance_type="ccx13", max_hourly_cost_micros=82_700),
        ProviderPurchaseLimit(region="ash", instance_type="ccx23", max_hourly_cost_micros=166_000),
        ProviderPurchaseLimit(region="ash", instance_type="ccx33", max_hourly_cost_micros=267_000),
        ProviderPurchaseLimit(region="ash", instance_type="ccx43", max_hourly_cost_micros=529_000),
        ProviderPurchaseLimit(
            region="ash", instance_type="ccx53", max_hourly_cost_micros=1_019_400
        ),
        ProviderPurchaseLimit(
            region="ash", instance_type="ccx63", max_hourly_cost_micros=1_643_000
        ),
    ),
    root_volume_gib=80,
    warm_cpu_min=1,
)
