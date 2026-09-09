from database.tables.compute import ComputeUnitTable
from database.tables.provider_launches import ProviderNodeLaunchTable
from psycopg.errors import LockNotAvailable
from shared.errors import ConflictError, UpstreamUnavailableError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session


class ProviderNodeLaunchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, launch_id: str, *, for_update: bool = False) -> ProviderNodeLaunchTable | None:
        statement = select(ProviderNodeLaunchTable).where(ProviderNodeLaunchTable.id == launch_id)
        if for_update:
            statement = statement.with_for_update(nowait=True)
        try:
            return self.session.scalar(statement)
        except OperationalError as exc:
            if isinstance(exc.orig, LockNotAvailable):
                raise UpstreamUnavailableError("provider launch operation is in progress") from None
            raise

    def lock_unit(self, unit_id: str) -> None:
        try:
            self.session.execute(
                select(ComputeUnitTable.id)
                .where(ComputeUnitTable.id == unit_id)
                .with_for_update(nowait=True)
            )
        except OperationalError as exc:
            if isinstance(exc.orig, LockNotAvailable):
                raise UpstreamUnavailableError(
                    "provider launch preparation is in progress"
                ) from None
            raise

    def active_slot(self, unit_id: str, server_name: str) -> ProviderNodeLaunchTable | None:
        return self.session.scalar(
            select(ProviderNodeLaunchTable).where(
                ProviderNodeLaunchTable.unit_id == unit_id,
                ProviderNodeLaunchTable.server_name == server_name,
                ProviderNodeLaunchTable.revoked_at.is_(None),
            )
        )

    def save(self, launch: ProviderNodeLaunchTable) -> None:
        self.session.add(launch)
        try:
            self.session.flush()
        except IntegrityError:
            raise ConflictError(
                "provider launch identity conflicts with an existing launch"
            ) from None
