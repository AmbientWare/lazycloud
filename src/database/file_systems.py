from sqlalchemy import Column, String, Integer
from src.database.base import BaseModel, BaseTable, DatabaseService

class FileSystemTable(BaseTable):
    """SQLAlchemy model for a file system"""

    __tablename__ = "file_systems"

    name = Column(String, nullable=False)
    image = Column(String, nullable=False)
    region = Column(String, nullable=False)
    size = Column(Integer, nullable=False)


class FileSystemPydantic(BaseModel):
    """Pydantic model for a file system"""

    name: str
    image: str
    region: str
    size: int


class FileSystemService(DatabaseService[FileSystemTable, FileSystemPydantic]):
    """Service layer for file system operations"""

    def __init__(self):
        super().__init__(FileSystemTable, FileSystemPydantic)
