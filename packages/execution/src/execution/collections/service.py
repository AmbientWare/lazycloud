from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from database.repositories.execution import QueueRepository
from pydantic import JsonValue
from shared.errors import NotFoundError
from shared.queue_messages import QueueMessage
from shared.timestamps import utc_now

from execution.context import ExecutionContext


@dataclass(slots=True)
class CollectionService:
    context: ExecutionContext

    def publish(
        self,
        queue: str,
        body: JsonValue,
        *,
        workspace: str = "default",
    ) -> QueueMessage:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = QueueRepository(session)
            return repository.messages.create(
                {
                    "queue": queue,
                    "body": body,
                    "attempts": 0,
                    "available_at": utc_now().isoformat(),
                },
                workspace_id=workspace_id,
            )

    def consume(
        self,
        queue: str,
        *,
        workspace: str = "default",
        lease_seconds: int = 60,
    ) -> QueueMessage | None:
        now = utc_now()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            return QueueRepository(session).claim_available_message(
                queue,
                workspace_id=workspace_id,
                now=now,
                lease_until=lease_until,
            )

    def ack(self, message_id: str, *, queue: str, workspace: str = "default") -> None:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = QueueRepository(session).messages
            match = next(
                (
                    message
                    for message in repository.list(workspace_id=workspace_id)
                    if message.id == message_id and message.queue == queue
                ),
                None,
            )
            if match is None:
                msg = f"queue message not found in {queue}: {message_id}"
                raise NotFoundError(msg)
            repository.delete(message_id, workspace_id=workspace_id)

    def queue_depth(self, queue: str, *, workspace_id: str | None = None) -> int:
        with self.context.database.session() as session:
            resolved_workspace_id = workspace_id or self.context.default_workspace_id(session)
            return QueueRepository(session).queue_depth(
                queue,
                workspace_id=resolved_workspace_id,
                now=utc_now(),
            )
