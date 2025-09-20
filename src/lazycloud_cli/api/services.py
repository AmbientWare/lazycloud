from lazycloud_cli.api.base import BaseAPI
from lazycloud_cli.api.tasks import TasksAPI
from shared.responses.tasks import ServiceTaskStatusResponse


class ServicesAPI(BaseAPI):
    def __init__(self):
        super().__init__("services")
        self._tasks = TasksAPI()

    def restart_service(
        self, deployment_id: str, service_name: str
    ) -> ServiceTaskStatusResponse:
        """Restart a specific service in a deployment."""
        response_data = self._post(f"/restart/{deployment_id}/{service_name}")
        return ServiceTaskStatusResponse(**response_data)

    def restart_all_services(self, deployment_id: str) -> ServiceTaskStatusResponse:
        """Restart all services in a deployment."""
        response_data = self._post(f"/restart/{deployment_id}")
        return ServiceTaskStatusResponse(**response_data)
