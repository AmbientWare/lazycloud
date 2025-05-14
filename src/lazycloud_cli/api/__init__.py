from lazycloud_cli.api.machines import machines_api
from lazycloud_cli.api.ssh_keys import ssh_keys_api
from lazycloud_cli.api.users import users_api
from lazycloud_cli.api.file_systems import file_systems_api
from lazycloud_cli.api.platform import platform_api

class API:
    def __init__(self):
        self.machines = machines_api
        self.ssh_keys = ssh_keys_api
        self.users = users_api
        self.file_systems = file_systems_api
        self.platform = platform_api

api = API()

__all__ = ["api"]
