from lazycloud_cli.api.deployments import deployments_api
from lazycloud_cli.api.logs import logs_api
from lazycloud_cli.api.secrets import secrets_api
from lazycloud_cli.api.status import status_api
from lazycloud_cli.api.tasks import tasks_api
from lazycloud_cli.api.users import users_api
from lazycloud_cli.api.versions import versions_api


class API:
    def __init__(self):
        self.users = users_api
        self.tasks = tasks_api
        self.deployments = deployments_api
        self.versions = versions_api
        self.secrets = secrets_api
        self.logs = logs_api
        self.status = status_api


api = API()

__all__ = ["api"]
