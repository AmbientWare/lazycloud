from dataclasses import dataclass

from machines.database.machines import MachineService
from machines.database.tokens import TokenService


@dataclass
class Database:
    machines: MachineService
    tokens: TokenService


db = Database(
    machines=MachineService(),
    tokens=TokenService(),
)

__all__ = ["db"]
