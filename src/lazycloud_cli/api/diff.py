from lazycloud_cli.api.base import BaseAPI
from shared.requests.deployments import DiffRequest
from shared.responses.deployments import DiffResponse


class DiffAPI(BaseAPI):
    def __init__(self):
        super().__init__("diff")

    def get_deployment_diff(
        self,
        deployment_id: str,
        compose_yaml: str,
        deployment_name: str | None = None,
        env_keys: list[str] | None = None,
    ) -> DiffResponse:
        """Get diff between current deployment and new compose file."""
        request = DiffRequest(
            compose_yaml=compose_yaml,
            deployment_name=deployment_name,
            env_keys=env_keys,
        )

        response = self._post(f"{deployment_id}", json=request.model_dump())
        return DiffResponse(**response)
