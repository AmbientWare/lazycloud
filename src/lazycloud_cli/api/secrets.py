"""
API client for secrets operations.
"""

from lazycloud_cli.api.base import BaseAPI
from shared.models.secrets import SecretCollection, SecretsRequest
from shared.responses.secrets import SecretsStoredResponse


class SecretsAPI(BaseAPI):
    def __init__(self):
        super().__init__("secrets")

    def store_secrets(
        self, deployment_id: str, secrets: SecretCollection
    ) -> SecretsStoredResponse:
        """Store secrets for a deployment."""
        request = SecretsRequest(secrets_collection=secrets)
        response_data = self._post(
            f"{deployment_id}",
            json=request.model_dump(),
        )
        return SecretsStoredResponse(**response_data)


# Create instance for other modules to import
secrets_api = SecretsAPI()
