from dataclasses import dataclass

from database.tables.orchestration import ContainerTable
from database.tables.previews import PreviewSessionTable
from foundation.ids import try_uuid
from shared.containers import LIVE_CONTAINER_STATUSES
from shared.http.previews import PreviewSessionResponse
from sqlalchemy import select
from sqlalchemy.orm import Session


@dataclass(slots=True)
class PreviewSessionRepository:
    session: Session

    def get(self, preview_id: str, *, lock: bool = False) -> PreviewSessionTable | None:
        identifier = try_uuid(preview_id)
        if identifier is None:
            return None
        statement = select(PreviewSessionTable).where(PreviewSessionTable.id == identifier)
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def for_stub(self, stub_id: str, *, lock: bool = False) -> PreviewSessionTable | None:
        statement = select(PreviewSessionTable).where(
            PreviewSessionTable.execution_stub_id == stub_id
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement.execution_options(populate_existing=True))

    def reconcile_candidates(self, *, limit: int) -> list[PreviewSessionResponse]:
        rows = self.session.scalars(
            select(PreviewSessionTable)
            .outerjoin(ContainerTable, ContainerTable.id == PreviewSessionTable.container_id)
            .where(
                (PreviewSessionTable.status == "active")
                | ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES])
            )
            .order_by(PreviewSessionTable.updated_at, PreviewSessionTable.id)
            .limit(max(limit, 0))
        )
        return [PreviewSessionResponse.model_validate(row, from_attributes=True) for row in rows]
