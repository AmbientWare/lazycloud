from lazycloud_cli.api.base import BaseAPI
from shared.requests.registry import UploadIntentRequest
from shared.responses.registry import UploadIntentResponse


class RegistryAPI(BaseAPI):
    def __init__(self):
        super().__init__("registry")

    def get_upload_intent(
        self, deployment_name: str, repo_name: str, session_name: str | None = None
    ) -> UploadIntentResponse:
        """Get upload intent for a repository in a deployment."""
        request = UploadIntentRequest(
            deployment_name=deployment_name,
            repo_name=repo_name,
            session_name=session_name,
        )
        response_data = self._post(
            "/upload-intent",
            json=request.model_dump(),
        )
        return UploadIntentResponse(**response_data)
