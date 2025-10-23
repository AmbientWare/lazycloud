from lazycloud_cli.api.base import BaseAPI
from lazycloud_cli.config import config
from shared.requests.registry import UploadIntentRequest
from shared.responses.registry import UploadIntentResponse


class RegistryAPI(BaseAPI):
    def __init__(self):
        super().__init__("workspaces")

    def get_upload_intent(
        self, deployment_name: str, repo_name: str, session_name: str | None = None
    ) -> UploadIntentResponse:
        """Get upload intent for a repository in a deployment."""

        workspace_id = config.active_workspace_id
        request = UploadIntentRequest(
            workspace_id=workspace_id,
            deployment_name=deployment_name,
            repo_name=repo_name,
            session_name=session_name,
        )

        response_data = self._post(
            f"/{workspace_id}/registry/upload-intent",
            json=request.model_dump(),
        )
        return UploadIntentResponse(**response_data)
