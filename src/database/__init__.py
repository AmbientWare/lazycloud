from dataclasses import dataclass

from src.database.machines import MachineService
from src.database.api_keys import ApiKeyService
from src.database.ssh_keys import SshKeyService
from src.database.usage import UsageService
from src.database.file_systems import FileSystemService


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
