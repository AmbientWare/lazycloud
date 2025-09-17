from dataclasses import dataclass

import nest_asyncio

from lazycloud_api.database.api_keys import ApiKeyService
from lazycloud_api.database.compose import ComposeDeploymentService
from lazycloud_api.database.secrets import SecretService
from lazycloud_api.database.usage import UsageService
from lazycloud_api.database.usage_period import UsagePeriodService

nest_asyncio.apply()


@dataclass
class Database:
    api_keys: ApiKeyService
    usage: UsageService
    usage_periods: UsagePeriodService
    compose_deployments: ComposeDeploymentService
    secrets: SecretService


db = Database(
    api_keys=ApiKeyService(),
    usage=UsageService(),
    usage_periods=UsagePeriodService(),
    compose_deployments=ComposeDeploymentService(),
    secrets=SecretService(),
)

__all__ = ["db"]
