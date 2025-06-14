from sqlalchemy import Column, String, Integer, ForeignKey
from sqlalchemy.orm import relationship
from enum import Enum
from typing import Optional

from lazycloud_api.database.base import BaseModel, BaseTable, DatabaseService


class VolumeTypes(str, Enum):
    """Types of volumes"""

    ROOT = "root"
    DATA = "data"


class VolumeTable(BaseTable):
    """SQLAlchemy model for a file system"""

    __tablename__ = "volumes"

    name = Column(String, nullable=False)
    region = Column(String, nullable=False)
    size = Column(Integer, nullable=False)
    mount_path = Column(String, default="/data", nullable=False)
    type = Column(String, default=VolumeTypes.DATA, nullable=False)

    # Back-reference to machine
    machine_id = Column(Integer, ForeignKey("machines.id"), nullable=False)
    machine = relationship("MachineTable", back_populates="volumes")


class VolumePydantic(BaseModel):
    """Pydantic model for a file system"""

    name: str
    region: str
    size: int
    mount_path: str = "/data"
    type: VolumeTypes = VolumeTypes.DATA
    machine_id: Optional[int] = None


class VolumeService(DatabaseService[VolumeTable, VolumePydantic]):
    """Service layer for file system operations"""

    def __init__(self):
        super().__init__(VolumeTable, VolumePydantic)
