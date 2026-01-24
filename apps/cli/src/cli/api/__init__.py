from cli.api.auth import AuthAPI
from cli.api.base import APIError
from cli.api.builds import BuildsAPI
from cli.api.deployments import DeploymentsAPI
from cli.api.diff import DiffAPI
from cli.api.feedback import FeedbackAPI
from cli.api.instances import InstancesAPI
from cli.api.logs import LogsAPI
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
        self.auth = AuthAPI()
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
        self.workspaces = WorkspacesAPI()
        self.usage = UsageAPI()
        self.builds = BuildsAPI()
        self.feedback = FeedbackAPI()


api = API()

__all__ = ["api", "APIError"]
