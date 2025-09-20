from lazycloud_cli.api.base import BaseAPI
from lazycloud_cli.api.tasks import TasksAPI
from shared.responses.deployments import (
    RestartResponse,
)


class ServicesAPI(BaseAPI):
    def __init__(self):
        super().__init__("services")
        self._tasks = TasksAPI()

    def restart_service(self, deployment_id: str, service_name: str) -> RestartResponse:
        """Restart a specific service in a deployment."""
        response_data = self._post(f"restart/{deployment_id}/{service_name}")
        return RestartResponse(**response_data)

    def restart_all_services(self, deployment_id: str) -> RestartResponse:
        """Restart all services in a deployment."""
        response_data = self._post(f"restart/{deployment_id}")
        return RestartResponse(**response_data)
