from dataclasses import dataclass

import nest_asyncio

from lazycloud_api.database.api_keys import ApiKeyService
from lazycloud_api.database.compose import ComposeDeploymentService
from lazycloud_api.database.secrets import SecretService
from lazycloud_api.database.users import UserService

nest_asyncio.apply()


@dataclass
class Database:
    api_keys: ApiKeyService
    users: UserService
    compose_deployments: ComposeDeploymentService
    secrets: SecretService


db = Database(
    api_keys=ApiKeyService(),
    users=UserService(),
    compose_deployments=ComposeDeploymentService(),
    secrets=SecretService(),
)

__all__ = ["db"]
