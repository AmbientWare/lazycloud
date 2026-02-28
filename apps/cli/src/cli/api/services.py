from datetime import UTC

from responses.services import ServiceStatusResponse
from responses.tasks import ServiceTaskStatusResponse

from cli.api.base import BaseAPI
from cli.api.tasks import TasksAPI


class ServicesAPI(BaseAPI):
    def __init__(self):
        # NOTE: services are nested under deployments
        super().__init__("deployments")
        self._tasks = TasksAPI()

    async def get_service_statuses(
        self, deployment_id: str
    ) -> list[ServiceStatusResponse]:
        """Get the status of all services in a deployment."""
        response_data = await self._get_async(f"/{deployment_id}/services")
        statuses = [ServiceStatusResponse(**data) for data in response_data]
        for status in statuses:
            status.service.last_checked = status.service.last_checked.replace(
                tzinfo=UTC
            ).astimezone()
        return statuses

    async def get_service_status(
        self, deployment_id: str, service_name: str, fast: bool = False
    ) -> ServiceStatusResponse:
        """Get the status of a specific service in a deployment."""
        params = {"fast": fast} if fast else None
        response_data = await self._get_async(
            f"/{deployment_id}/services/{service_name}/status",
            params=params,
        )
        status = ServiceStatusResponse(**response_data)
        status.service.last_checked = status.service.last_checked.replace(
            tzinfo=UTC
        ).astimezone()
        return status

    def restart_service(
        self, deployment_id: str, service_name: str
    ) -> ServiceTaskStatusResponse:
        """Restart a specific service in a deployment."""
        response_data = self._post(f"/{deployment_id}/services/{service_name}/restart")
        return ServiceTaskStatusResponse(**response_data)

    def restart_all_services(self, deployment_id: str) -> ServiceTaskStatusResponse:
        """Restart all services in a deployment."""
        response_data = self._post(f"/{deployment_id}/services/restart")
        return ServiceTaskStatusResponse(**response_data)
