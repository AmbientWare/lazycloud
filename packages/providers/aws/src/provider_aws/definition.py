from compute.aws_configuration import AWS_COMPUTE_CONFIGURATION
from compute.providers import ProviderCapacityPolicy, ProviderDefinition

from provider_aws.instance_catalog import AWS_ALLOWED_OFFERS

AWS_PROVIDER = ProviderDefinition(
    kind="aws",
    policy=ProviderCapacityPolicy(
        purchases_enabled=True,
        default_region=AWS_COMPUTE_CONFIGURATION.default_region,
        allowed_regions=AWS_COMPUTE_CONFIGURATION.allowed_regions,
        allowed_offers=AWS_ALLOWED_OFFERS,
        root_volume_gib=AWS_COMPUTE_CONFIGURATION.root_volume_gib,
        idle_timeout_seconds=AWS_COMPUTE_CONFIGURATION.idle_timeout_seconds,
    ),
)
