from dataclasses import dataclass

from machines.database.machines import MachineService
from machines.database.api_keys import ApiKeyService
from machines.database.ssh_keys import SshKeyService


@dataclass
class Database:
    machines: MachineService
    api_keys: ApiKeyService
    ssh_keys: SshKeyService


db = Database(
    machines=MachineService(), api_keys=ApiKeyService(), ssh_keys=SshKeyService()
)

__all__ = ["db"]
