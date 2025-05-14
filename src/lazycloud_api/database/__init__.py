from dataclasses import dataclass

from lazycloud_api.database.machines import MachineService
from lazycloud_api.database.api_keys import ApiKeyService
from lazycloud_api.database.ssh_keys import SshKeyService
from lazycloud_api.database.usage import UsageService
from lazycloud_api.database.file_systems import FileSystemService


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
