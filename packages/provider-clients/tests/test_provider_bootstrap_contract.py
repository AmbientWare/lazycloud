from __future__ import annotations

from provider_aws import AwsProviderSettings
from pydantic import BaseModel

_PROVIDER_SETTINGS: tuple[tuple[type[BaseModel], dict[str, object]], ...] = (
    (AwsProviderSettings, {"region": "us-east-1"}),
)
