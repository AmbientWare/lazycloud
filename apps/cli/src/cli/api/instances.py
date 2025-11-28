from responses.tasks import InstanceTaskStatusResponse

from cli.api.base import BaseAPI


class InstancesAPI(BaseAPI):
    def __init__(self):
        super().__init__("deployments")

    def delete_instance(
        self, deployment_id: str, pod_name: str, force: bool = False
    ) -> InstanceTaskStatusResponse:
        """Delete a specific instance in a deployment"""
        params = {"force": "true"} if force else {}
        response_data = self._delete(
            f"/{deployment_id}/instances/{pod_name}", params=params
        )
        return InstanceTaskStatusResponse(**response_data)
