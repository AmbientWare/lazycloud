from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderBootstrapNodeEvidence:
    instance_id: str
    region: str


@dataclass(frozen=True, slots=True)
class ProviderBootstrapNodeIdentityTarget:
    provider_ref: str
    unit_id: str
    launch_id: str
    region: str
