from lazycloud_cli.api.base import BaseAPI
from shared.requests.deployments import DiffRequest, DiffType
from shared.responses.deployments import DiffResponse


class DiffAPI(BaseAPI):
    def __init__(self):
        super().__init__("diff")

    def get_deployment_diff(
        self,
        diff_type: DiffType,
        workspace_id: str,
        deployment_name: str,
        compose_yaml: str,
        env_keys: list[str] | None = None,
    ) -> DiffResponse:
        """Get diff for a deployment by name."""
        request = DiffRequest(
            diff_type=diff_type,
            workspace_id=workspace_id,
            deployment_name=deployment_name,
            compose_yaml=compose_yaml,
            env_keys=env_keys or [],
        )

        response = self._post("", json=request.model_dump())
        return DiffResponse(**response)
