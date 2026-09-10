from types import MappingProxyType

from provider_aws.definition import AWS_PROVIDER
from provider_hetzner.capacity_policy import HETZNER_PROVIDER

PROVIDER_DEFINITIONS = MappingProxyType(
    {definition.kind: definition for definition in (AWS_PROVIDER, HETZNER_PROVIDER)}
)
