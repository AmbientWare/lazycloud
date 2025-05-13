from dataclasses import dataclass

from database.machines import MachineService
from database.api_keys import ApiKeyService
from database.ssh_keys import SshKeyService
from database.usage import UsageService
from database.file_systems import FileSystemService


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
