from dataclasses import dataclass

from lazycloud_api.database.machines import MachineService
from lazycloud_api.database.api_keys import ApiKeyService
from lazycloud_api.database.ssh_keys import SshKeyService
from lazycloud_api.database.usage import UsageService
from lazycloud_api.database.usage_period import UsagePeriodService
from lazycloud_api.database.volumes import VolumeService


@dataclass
class Database:
    machines: MachineService
    api_keys: ApiKeyService
    ssh_keys: SshKeyService
    usage: UsageService
    usage_periods: UsagePeriodService
    volumes: VolumeService


db = Database(
    machines=MachineService(),
    api_keys=ApiKeyService(),
    ssh_keys=SshKeyService(),
    usage=UsageService(),
    usage_periods=UsagePeriodService(),
    volumes=VolumeService(),
)

__all__ = ["db"]
