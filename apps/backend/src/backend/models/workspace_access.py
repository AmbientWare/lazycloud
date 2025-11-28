from backend.database.user_workspaces import UserWorkspacePydantic
from backend.database.users import UserPydantic
from backend.database.workspaces import WorkspacePydantic
from pydantic import BaseModel


class WorkspaceAccess(BaseModel):
    """Container for workspace access information"""

    membership: UserWorkspacePydantic
    workspace: WorkspacePydantic
    user: UserPydantic
