from sqlalchemy import Column, String

from machines.database.base import BaseModel, BaseTable, DatabaseService


class SshKeyTable(BaseTable):
    """SQLAlchemy model for a ssh key"""

    __tablename__ = "ssh_keys"

    name = Column(String, nullable=False, unique=True, index=True)
    value = Column(String, nullable=False, unique=True, index=True)


class SshKeyPydantic(BaseModel):
    """Pydantic model for a ssh key"""

    name: str
    value: str


class SshKeyService(DatabaseService[SshKeyTable, SshKeyPydantic]):
    """Service layer for api key operations"""

    def __init__(self):
        super().__init__(SshKeyTable, SshKeyPydantic)
