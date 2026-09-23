from types import MappingProxyType

from provider_aws.definition import AWS_PROVIDER

PROVIDER_DEFINITIONS = MappingProxyType({AWS_PROVIDER.kind: AWS_PROVIDER})
