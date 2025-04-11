from cli.api.machines import machines_api
from cli.api.ssh_keys import ssh_keys_api
from cli.api.users import users_api

class API:
    def __init__(self):
        self.machines = machines_api
        self.ssh_keys = ssh_keys_api
        self.users = users_api


api = API()

__all__ = ["api"]
