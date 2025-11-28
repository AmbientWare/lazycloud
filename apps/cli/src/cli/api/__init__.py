from cli.api.base import APIError
from cli.api.builds import BuildsAPI
from cli.api.deployments import DeploymentsAPI
from cli.api.diff import DiffAPI
from cli.api.instances import InstancesAPI
from cli.api.logs import LogsAPI
from cli.api.registry import RegistryAPI
from cli.api.secrets import SecretsAPI
from cli.api.services import ServicesAPI
from cli.api.status import StatusAPI
from cli.api.tasks import TasksAPI
from cli.api.usage import UsageAPI
from cli.api.users import UsersAPI
from cli.api.versions import VersionsAPI
from cli.api.workspaces import WorkspacesAPI


class API:
    def __init__(self):
        self.users = UsersAPI()
        self.tasks = TasksAPI()
        self.deployments = DeploymentsAPI()
        self.versions = VersionsAPI()
        self.secrets = SecretsAPI()
        self.logs = LogsAPI()
        self.status = StatusAPI()
        self.diff = DiffAPI()
        self.instances = InstancesAPI()
        self.services = ServicesAPI()
        self.registry = RegistryAPI()
        self.workspaces = WorkspacesAPI()
        self.usage = UsageAPI()
        self.builds = BuildsAPI()


api = API()

__all__ = ["api", "APIError"]
