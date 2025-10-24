from lazycloud_cli.api.base import BaseAPI
from shared.models.secrets import Secret
from shared.requests.secrets import SecretsRequest
from shared.responses.secrets import SecretsResponse, SecretsStoredResponse


class SecretsAPI(BaseAPI):
    def __init__(self):
        super().__init__("deployments")

    def get_secrets(
        self, deployment_id: str, show_values: bool = False
    ) -> SecretsResponse:
        """Get secrets for a deployment."""
        params = {"show_values": show_values} if show_values else {}
        response_data = self._get(f"/{deployment_id}/secrets", params=params)
        return SecretsResponse(**response_data)

    def get_secret_value(self, deployment_id: str, key: str) -> str:
        """Get a specific secret value for a deployment."""
        return self._get(f"/{deployment_id}/secrets/value/{key}")

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
