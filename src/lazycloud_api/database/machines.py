from typing import List, Optional
from sqlalchemy import Column, String, Integer
from sqlalchemy.orm import relationship
from enum import Enum
from lazycloud_api.database.base import BaseModel, BaseTable, DatabaseService


class MachineStatus(str, Enum):
    """Status of a machine"""

    INITIALIZING = "Initializing"
    INITIALIZED = "Initialized"
    NETWORKING = "Setting up network"
    BUILDING = "Building machine"
    VOLUME = "Creating file system"
    VM_CREATING = "Creating virtual machine"
    DEPLOYED = "Deployed"
    DELETING = "Deleting"


class MachineTable(BaseTable):
    """SQLAlchemy model for a machine"""

    __tablename__ = "machines"

    name = Column(String, nullable=False)
    region = Column(String, nullable=False)
    image = Column(String, nullable=False)
    cpu_kind = Column(String, nullable=False)
    cpu = Column(Integer, nullable=False)
    gpu_kind = Column(String, nullable=True)
    memory = Column(Integer, nullable=False)
    status = Column(String, nullable=False)
    app_port = Column(Integer, nullable=False)

    # One-to-many relationship with volumes
    volumes = relationship(
        "VolumeTable", back_populates="machine", cascade="all, delete-orphan"
    )


class MachinePydantic(BaseModel):
    """Pydantic model for a machine"""

    name: str
    region: str
    image: str
    cpu_kind: str
    cpu: int
    gpu_kind: str | None = None
    memory: int
    status: MachineStatus
    app_port: int


class MachineService(DatabaseService[MachineTable, MachinePydantic]):
    """Service layer for machine operations"""

    def __init__(self):
        super().__init__(MachineTable, MachinePydantic)

    async def update_machine_status(
        self, machine_id: int, status: MachineStatus
    ) -> None:
        """Update the status of a machine"""
        machine = await self.aget_by_id(machine_id)
        if not machine:
            raise ValueError(f"Machine {machine_id} not found")

        machine.status = status
        await self.aupdate(machine)

    async def get_first_available_port(self, usage_uuid: str) -> int:
        """Get the first available port"""
        deployted_machines = await self.afind(
            filters={"status": MachineStatus.DEPLOYED, "usage_uuid": usage_uuid}
        )
        used_ports = [machine.app_port for machine in deployted_machines]
        # port start from 10022
        for port in range(10022, 65535):
            if port not in used_ports:
                return port

        raise ValueError("No available ports")

    async def asearch(
        self,
        user_id: Optional[str] = None,
        region: Optional[str] = None,
        status: Optional[str] = None,
        min_cpu: Optional[int] = None,
        max_cpu: Optional[int] = None,
        min_memory: Optional[int] = None,
        max_memory: Optional[int] = None,
        gpu_kind: Optional[str] = None,
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
        if gpu_kind is not None:
            filters["gpu_kind"] = gpu_kind

        return await self.afind(**filters)
