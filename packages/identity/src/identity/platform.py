from dataclasses import dataclass
from functools import cached_property
from uuid import uuid4

from database.repositories.identity import WorkspaceRepository, new_signing_key
from shared.errors import NotFoundError
from shared.identity import WorkspaceKind, WorkspaceRecord

from database import DatabaseClient


@dataclass(frozen=True)
class PlatformNamespaceService:
    database: DatabaseClient

    @cached_property
    def namespace_id(self) -> str:
        return self.get().id

    def initialize(self) -> WorkspaceRecord:
        with self.database.session() as session:
            repository = WorkspaceRepository(session)
            repository.lock_platform_initialization()
            current = repository.platform()
            if current is not None:
                return current
            identity = str(uuid4())
            return repository.upsert(
                WorkspaceRecord(
                    id=identity,
                    name=f"platform-{identity}",
                    kind=WorkspaceKind.Platform,
                    signing_key=new_signing_key(),
                )
            )

    def get(self) -> WorkspaceRecord:
        with self.database.session() as session:
            current = WorkspaceRepository(session).platform()
        if current is None:
            raise NotFoundError(
                "platform namespace is not initialized; run lazycloud-admin platform initialize"
            )
        return current
