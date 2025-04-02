from typing import List, Optional
from sqlalchemy import Column, String, Integer
from enum import Enum
from machines.database.base import BaseModel, BaseTable, DatabaseService


class MachineStatus(str, Enum):
    """Status of a machine"""

    DEPLOYING = "deploying"
    DEPLOYED = "deployed"
    DELETING = "deleting"


class MachineTable(BaseTable):
    """SQLAlchemy model for a machine"""

    __tablename__ = "machines"

    name = Column(String, nullable=False)
    region = Column(String, nullable=False)
    image = Column(String, nullable=False)
    cpu_kind = Column(String, nullable=False)
    cpu = Column(Integer, nullable=False)
    memory = Column(Integer, nullable=False)
    volume_size = Column(Integer, nullable=False)
    status = Column(String, nullable=False)


class MachinePydantic(BaseModel):
    """Pydantic model for a machine"""

    name: str
    region: str
    image: str
    cpu_kind: str
    cpu: int
    memory: int
    status: MachineStatus
    volume_size: int


class MachineService(DatabaseService[MachineTable, MachinePydantic]):
    """Service layer for machine operations"""

    def __init__(self):
        super().__init__(MachineTable, MachinePydantic)

    async def asearch(
        self,
        user_id: Optional[str] = None,
        region: Optional[str] = None,
        status: Optional[str] = None,
        min_cpu: Optional[int] = None,
        max_cpu: Optional[int] = None,
        min_memory: Optional[int] = None,
        max_memory: Optional[int] = None,
    ) -> List[MachinePydantic]:
        """Advanced search with multiple filters"""
        filters = {}

        if user_id:
            filters["user_id"] = user_id
        if region:
            filters["region"] = region
        if status:
            filters["status"] = status
        if min_cpu is not None:
            filters["cpu__gte"] = min_cpu
        if max_cpu is not None:
            filters["cpu__lte"] = max_cpu
        if min_memory is not None:
            filters["memory__gte"] = min_memory
        if max_memory is not None:
            filters["memory__lte"] = max_memory

        return await self.afind(**filters)
