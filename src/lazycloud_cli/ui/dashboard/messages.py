from textual.message import Message

from shared.models.statuses import ServiceStatus


class DeploymentSelected(Message):
    """Message emitted when a deployment is selected."""

    def __init__(self, deployment_id: str, deployment_name: str):
        super().__init__()
        self.deployment_id = deployment_id
        self.deployment_name = deployment_name


class ServiceSelected(Message):
    """Message emitted when a service is selected."""

    def __init__(
        self,
        deployment_id: str,
        deployment_name: str,
        service: ServiceStatus,
    ):
        super().__init__()
        self.deployment_id = deployment_id
        self.deployment_name = deployment_name
        self.service = service
