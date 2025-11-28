from api_requests.secrets import SecretsRequest
from models.secrets import Secret
from responses.secrets import SecretsResponse, SecretsStoredResponse

from cli.api.base import BaseAPI


class SecretsAPI(BaseAPI):
    def __init__(self):
        super().__init__("deployments")

    async def get_secrets(
        self, deployment_id: str, show_values: bool = False
    ) -> SecretsResponse:
        """Get secrets for a deployment."""
        params = {"show_values": show_values} if show_values else {}
        response_data = await self._get_async(f"/{deployment_id}/secrets", params=params)
        return SecretsResponse(**response_data)

    async def get_secret_value(self, deployment_id: str, key: str) -> str:
        """Get a specific secret value for a deployment."""
        return await self._get_async(f"/{deployment_id}/secrets/value/{key}")

    def update_secrets(
        self, deployment_id: str, secrets: list[Secret]
    ) -> SecretsStoredResponse:
        """Update existing secrets for a deployment"""
        request_data = SecretsRequest(secrets=secrets)
        response_data = self._patch(
            f"/{deployment_id}/secrets",
            json=request_data.model_dump(),
        )
        return SecretsStoredResponse(**response_data)

    def delete_secrets(
        self, deployment_id: str, secrets: list[Secret]
    ) -> SecretsStoredResponse:
        """Delete existing secrets for a deployment"""
        request_data = SecretsRequest(secrets=secrets)
        response_data = self._delete(
            f"/{deployment_id}/secrets",
            json=request_data.model_dump(),
        )
        return SecretsStoredResponse(**response_data)

    def store_secrets(
        self, deployment_id: str, secrets: list[Secret]
    ) -> SecretsStoredResponse:
        """Create new secrets for a deployment"""
        request_data = SecretsRequest(secrets=secrets)
        response_data = self._post(
            f"/{deployment_id}/secrets",
            json=request_data.model_dump(),
        )
        return SecretsStoredResponse(**response_data)
