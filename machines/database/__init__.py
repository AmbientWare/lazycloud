from dataclasses import dataclass

from machines.database.machines import MachineService
from machines.database.api_keys import ApiKeyService


@dataclass
class Database:
    machines: MachineService
    api_keys: ApiKeyService


db = Database(
    machines=MachineService(),
    api_keys=ApiKeyService(),
)

__all__ = ["db"]
