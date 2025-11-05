from pydantic import BaseModel

from lazycloud_api.database.user_workspaces import UserWorkspacePydantic
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.database.workspaces import WorkspacePydantic


class WorkspaceAccess(BaseModel):
    """Container for workspace access information"""

    membership: UserWorkspacePydantic
    workspace: WorkspacePydantic
    user: UserPydantic
