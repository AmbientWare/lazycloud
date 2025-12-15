"""Custom messages for LazyCloud TUI component communication.

This module defines custom messages that enable decoupled component communication
following Textual best practices. Components post messages instead of directly
calling methods on other components.
"""

from textual.message import Message

from models.statuses import DeploymentStatus, ServiceStatus
from responses.deployments import DeploymentResponse
from cli.ui.textual.dashboard.containers.details.container import DisplayMode


class DeploymentSelected(Message):
    """Posted when a deployment is selected in the deployments list.

    Attributes:
        deployment: The selected deployment data
        status: The deployment's current status
    """

    def __init__(
        self,
        deployment: DeploymentResponse,
        status: DeploymentStatus | None = None,
    ) -> None:
        super().__init__()
        self.deployment = deployment
        self.status = status


class ServiceSelected(Message):
    """Posted when a service is selected in the services list.

    Attributes:
        deployment_id: ID of the deployment containing the service
        service_status: Status information for the selected service
    """

    def __init__(
        self,
        deployment_id: str,
        service_status: ServiceStatus,
    ) -> None:
        super().__init__()
        self.deployment_id = deployment_id
        self.service_status = service_status


class SecretSelected(Message):
    """Posted when a secret is selected in the secrets list.

    Attributes:
        deployment_id: ID of the deployment containing the secret
        secret_key: The secret key that was selected
    """

    def __init__(
        self,
        deployment_id: str,
        secret_key: str,
    ) -> None:
        super().__init__()
        self.deployment_id = deployment_id
        self.secret_key = secret_key


class DisplayModeChanged(Message):
    """Posted when the content container's display mode changes.

    Attributes:
        mode: The new display mode
    """

    def __init__(self, mode: DisplayMode) -> None:
        super().__init__()
        self.mode = mode


class DeploymentsLoaded(Message):
    """Posted when deployments have been loaded from the API.

    Attributes:
        has_deployments: Whether any deployments were found
    """

    def __init__(self, has_deployments: bool) -> None:
        super().__init__()
        self.has_deployments = has_deployments
