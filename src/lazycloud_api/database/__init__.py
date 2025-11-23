from dataclasses import dataclass

from lazycloud_api.database.api_keys import ApiKeyService
from lazycloud_api.database.compose import ComposeDeploymentService
from lazycloud_api.database.invitations import WorkspaceInvitationService
from lazycloud_api.database.secrets import SecretService
from lazycloud_api.database.usage import UsageService
from lazycloud_api.database.user_workspaces import UserWorkspaceService
from lazycloud_api.database.users import UserService
from lazycloud_api.database.workspaces import WorkspaceService


@dataclass
class Database:
    api_keys: ApiKeyService
    users: UserService
    compose_deployments: ComposeDeploymentService
    secrets: SecretService
    workspaces: WorkspaceService
    user_workspaces: UserWorkspaceService
    usage: UsageService
    invitations: WorkspaceInvitationService


db = Database(
    api_keys=ApiKeyService(),
    users=UserService(),
    compose_deployments=ComposeDeploymentService(),
    secrets=SecretService(),
    workspaces=WorkspaceService(),
    user_workspaces=UserWorkspaceService(),
    usage=UsageService(),
    invitations=WorkspaceInvitationService(),
)

__all__ = ["db"]
