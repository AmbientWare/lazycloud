from __future__ import annotations

from dataclasses import dataclass

from database.tables.images import ImageBuildLogTable, ImageBuildTable
from shared.errors import ConflictError, NotFoundError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session


@dataclass(slots=True)
class ImageBuildLogRepository:
    session: Session

    def append(self, build_id: str, *, workspace_id: str, after: int, messages: list[str]) -> int:
        row = self.session.scalar(
            select(ImageBuildTable)
            .where(ImageBuildTable.id == build_id, ImageBuildTable.workspace_id == workspace_id)
            .with_for_update()
        )
        if row is None:
            raise NotFoundError("image build does not exist")
        last = self.last_sequence(build_id)
        if after > last:
            raise ConflictError("image build progress contains a gap")
        if row.status not in {"pending", "running"}:
            return last
        if messages:
            self.session.execute(
                insert(ImageBuildLogTable)
                .values(
                    [
                        {"build_id": build_id, "sequence": after + index, "message": message}
                        for index, message in enumerate(messages, start=1)
                    ]
                )
                .on_conflict_do_nothing()
            )
        return max(last, after + len(messages))

    def last_sequence(self, build_id: str) -> int:
        return (
            self.session.scalar(
                select(func.coalesce(func.max(ImageBuildLogTable.sequence), 0)).where(
                    ImageBuildLogTable.build_id == build_id
                )
            )
            or 0
        )

    def page(self, build_id: str, *, after: int, limit: int = 256) -> list[tuple[int, str]]:
        rows = self.session.execute(
            select(ImageBuildLogTable.sequence, ImageBuildLogTable.message)
            .where(ImageBuildLogTable.build_id == build_id, ImageBuildLogTable.sequence > after)
            .order_by(ImageBuildLogTable.sequence)
            .limit(limit)
        )
        return [(row.sequence, row.message) for row in rows]
