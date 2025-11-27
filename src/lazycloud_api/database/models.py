"""All database models - import this to register models with SQLAlchemy metadata."""

from lazycloud_api.database.api_keys import ApiKeyTable
from lazycloud_api.database.compose import ComposeDeploymentTable
from lazycloud_api.database.invitations import WorkspaceInvitationTable
from lazycloud_api.database.secrets import SecretTable
from lazycloud_api.database.usage import (
    BuildUsageBreakdownTable,
    ComputeUsageBreakdownTable,
    NetworkingUsageBreakdownTable,
    StorageUsageBreakdownTable,
    UsageRecordTable,
)
from lazycloud_api.database.user_workspaces import UserWorkspaceTable
from lazycloud_api.database.users import UserTable
from lazycloud_api.database.workspaces import WorkspaceTable

__all__ = [
    "ApiKeyTable",
    "ComposeDeploymentTable",
    "WorkspaceInvitationTable",
    "SecretTable",
    "UsageRecordTable",
    "ComputeUsageBreakdownTable",
    "StorageUsageBreakdownTable",
    "NetworkingUsageBreakdownTable",
    "BuildUsageBreakdownTable",
    "UserWorkspaceTable",
    "UserTable",
    "WorkspaceTable",
]
