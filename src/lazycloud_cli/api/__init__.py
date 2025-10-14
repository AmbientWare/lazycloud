from lazycloud_cli.api.deployments import DeploymentsAPI
from lazycloud_cli.api.diff import DiffAPI
from lazycloud_cli.api.instances import InstancesAPI
from lazycloud_cli.api.logs import LogsAPI
from lazycloud_cli.api.registry import RegistryAPI
from lazycloud_cli.api.secrets import SecretsAPI
from lazycloud_cli.api.services import ServicesAPI
from lazycloud_cli.api.status import StatusAPI
from lazycloud_cli.api.tasks import TasksAPI
from lazycloud_cli.api.users import UsersAPI
from lazycloud_cli.api.versions import VersionsAPI


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


api = API()

__all__ = ["api"]
