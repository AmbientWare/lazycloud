from lazycloud_cli.api.base import BaseAPI


class WorkspacesAPI(BaseAPI):
    def __init__(self):
        super().__init__("workspaces")

    def list_workspaces(self) -> list[dict]:
        """List all workspaces user has access to"""
        return self._get(path="")

    def create_workspace(self, name: str) -> dict:
        """Create a new workspace"""
        return self._post(path="", json={"name": name})

    def delete_workspace(self, workspace_id: str) -> dict:
        """Delete a workspace"""
        return self._delete(path=f"/{workspace_id}")

    def get_workspace_by_name(self, name: str) -> dict | None:
        """Find workspace by name from user's workspace list"""
        workspaces = self.list_workspaces()
        for ws in workspaces:
            if ws.get("name") == name:
                return ws
        return None
