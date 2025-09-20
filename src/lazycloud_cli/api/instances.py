from lazycloud_cli.api.base import BaseAPI
from shared.responses.tasks import InstanceTaskStatusResponse


class InstancesAPI(BaseAPI):
    def __init__(self):
        super().__init__("instances")

    def delete_instance(
        self, deployment_id: str, service_name: str, pod_name: str
    ) -> InstanceTaskStatusResponse:
        """Delete a specific instance in a deployment."""
        response_data = self._delete(f"/{deployment_id}/{service_name}/{pod_name}")
        return InstanceTaskStatusResponse(**response_data)
