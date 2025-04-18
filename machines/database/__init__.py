from dataclasses import dataclass

from machines.database.machines import MachineService
from machines.database.api_keys import ApiKeyService
from machines.database.ssh_keys import SshKeyService
from machines.database.usage import UsageService
from machines.database.file_systems import FileSystemService


@dataclass
class Database:
    machines: MachineService
    api_keys: ApiKeyService
    ssh_keys: SshKeyService
    usage: UsageService
    file_systems: FileSystemService


db = Database(
    machines=MachineService(),
    api_keys=ApiKeyService(),
    ssh_keys=SshKeyService(),
    usage=UsageService(),
    file_systems=FileSystemService(),
)

__all__ = ["db"]
