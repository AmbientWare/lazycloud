from api_requests.registry import ImageExistsRequest, UploadIntentRequest
from responses.registry import ImageExistsResponse, UploadIntentResponse

from cli.api.base import BaseAPI
from cli.config import config


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

    def check_images_exist(
        self, deployment_name: str, image_names: list[str]
    ) -> ImageExistsResponse:
        """Check if images exist in the registry."""

        workspace_id = config.active_workspace_id
        request = ImageExistsRequest(
            deployment_name=deployment_name,
            image_names=image_names,
        )

        response_data = self._post(
            f"/{workspace_id}/registry/images-exist",
            json=request.model_dump(),
        )
        return ImageExistsResponse(**response_data)
