from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.mappers.aws_connections import (
    connection_from_row,
    tombstone_from_row,
    write_connection,
    write_tombstone,
)
from database.tables.aws_connections import (
    AwsAccountConnectionTable,
    AwsAuthorizationCleanupTombstoneTable,
)
from database.tables.identity import WorkspaceMemberTable
from shared.aws_connections import AwsAccountConnection, AwsAuthorizationCleanupTombstone
from shared.errors import ConflictError
from shared.identity import WorkspaceRole
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session


@dataclass(slots=True)
class AwsAccountConnectionRepository:
    session: Session

    def create(self, connection: AwsAccountConnection) -> AwsAccountConnection:
        connection = AwsAccountConnection.model_validate(dict(connection))
        row = AwsAccountConnectionTable(id=connection.id)
        write_connection(row, connection)
        self.session.add(row)
        self.session.flush()
        return connection

    def save(self, connection: AwsAccountConnection) -> AwsAccountConnection:
        row = self.session.get(AwsAccountConnectionTable, connection.id)
        if row is None:
            raise LookupError(f"AWS account connection {connection.id} does not exist")
        self._write(row, connection)
        return connection

    def get_for_user(
        self,
        user_id: str,
        *,
        for_update: bool = False,
    ) -> AwsAccountConnection | None:
        statement = select(AwsAccountConnectionTable).where(
            AwsAccountConnectionTable.user_id == user_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return connection_from_row(row) if row is not None else None

    def get_for_workspace_owner(
        self,
        workspace_id: str,
        *,
        for_update: bool = False,
    ) -> AwsAccountConnection | None:
        """The connected account backing a workspace, reached through its owner.

        One join rather than two lookups so the owner cannot change between them, and
        so every caller asks the question the same way.
        """
        statement = (
            select(AwsAccountConnectionTable)
            .join(
                WorkspaceMemberTable,
                WorkspaceMemberTable.user_id == AwsAccountConnectionTable.user_id,
            )
            .where(
                WorkspaceMemberTable.workspace_id == workspace_id,
                WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
            )
        )
        if for_update:
            statement = statement.with_for_update(of=AwsAccountConnectionTable)
        row = self.session.scalars(statement).first()
        return connection_from_row(row) if row is not None else None

    def get(
        self,
        connection_id: str,
        *,
        for_update: bool = False,
    ) -> AwsAccountConnection | None:
        statement = select(AwsAccountConnectionTable).where(
            AwsAccountConnectionTable.id == connection_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return connection_from_row(row) if row is not None else None

    def list_for_user(self, user_id: str) -> list[AwsAccountConnection]:
        statement = (
            select(AwsAccountConnectionTable)
            .where(AwsAccountConnectionTable.user_id == user_id)
            .order_by(
                AwsAccountConnectionTable.created_at.desc(),
                AwsAccountConnectionTable.id.asc(),
            )
        )
        return [connection_from_row(row) for row in self.session.scalars(statement)]

    def list_all(self) -> list[AwsAccountConnection]:
        """Every connected account, for control-plane-wide reconciliation."""
        statement = select(AwsAccountConnectionTable).order_by(
            AwsAccountConnectionTable.created_at.asc(),
            AwsAccountConnectionTable.id.asc(),
        )
        return [connection_from_row(row) for row in self.session.scalars(statement)]

    def claim_due(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> list[AwsAccountConnection]:
        statement = (
            select(AwsAccountConnectionTable)
            .where(
                AwsAccountConnectionTable.next_reconcile_at.is_not(None),
                AwsAccountConnectionTable.next_reconcile_at <= now,
                or_(
                    AwsAccountConnectionTable.claim_expires_at.is_(None),
                    AwsAccountConnectionTable.claim_expires_at <= now,
                ),
            )
            .order_by(
                AwsAccountConnectionTable.next_reconcile_at.asc(),
                AwsAccountConnectionTable.id.asc(),
            )
            .limit(max(limit, 1))
            .with_for_update(skip_locked=True)
        )
        claimed: list[AwsAccountConnection] = []
        for row in self.session.scalars(statement):
            current = connection_from_row(row)
            connection = current.model_copy(
                update={
                    "claim_token": str(uuid4()),
                    "claim_expires_at": lease_until,
                    "updated_at": now,
                }
            )
            self._write(row, connection)
            claimed.append(connection)
        return claimed

    def finish_claim(
        self,
        claimed: AwsAccountConnection,
        updated: AwsAccountConnection,
    ) -> AwsAccountConnection | None:
        row = self._claimed_row(claimed)
        if row is None:
            return None
        finished = updated.model_copy(
            update={
                "revision": claimed.revision + 1,
                "claim_token": None,
                "claim_expires_at": None,
            }
        )
        self._write(row, finished)
        return finished

    def delete_claimed(self, claimed: AwsAccountConnection) -> bool:
        row = self._claimed_row(claimed)
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    def delete(self, connection: AwsAccountConnection) -> None:
        self.session.execute(
            delete(AwsAccountConnectionTable).where(AwsAccountConnectionTable.id == connection.id)
        )
        self.session.flush()

    def _claimed_row(
        self,
        claimed: AwsAccountConnection,
    ) -> AwsAccountConnectionTable | None:
        row = self.session.scalar(
            select(AwsAccountConnectionTable)
            .where(
                AwsAccountConnectionTable.id == claimed.id,
            )
            .with_for_update()
        )
        if (
            row is None
            or row.revision != claimed.revision
            or row.claim_token != claimed.claim_token
        ):
            return None
        return row

    def _write(
        self,
        row: AwsAccountConnectionTable,
        connection: AwsAccountConnection,
    ) -> None:
        connection = AwsAccountConnection.model_validate(dict(connection))
        if (
            row.id != connection.id
            or row.user_id != connection.user_id
            or row.account_id != connection.account_id
            or row.external_id != connection.external_id
            or row.pool != connection.pool
        ):
            raise ConflictError("AWS connection identity cannot change")
        write_connection(row, connection)
        self.session.flush()


@dataclass(slots=True)
class AwsAuthorizationCleanupTombstoneRepository:
    session: Session

    def create(
        self,
        tombstone: AwsAuthorizationCleanupTombstone,
    ) -> AwsAuthorizationCleanupTombstone:
        tombstone = AwsAuthorizationCleanupTombstone.model_validate(dict(tombstone))
        row = AwsAuthorizationCleanupTombstoneTable(id=tombstone.id)
        write_tombstone(row, tombstone)
        self.session.add(row)
        self.session.flush()
        return tombstone

    def claim_due(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> list[AwsAuthorizationCleanupTombstone]:
        statement = (
            select(AwsAuthorizationCleanupTombstoneTable)
            .where(
                AwsAuthorizationCleanupTombstoneTable.next_reconcile_at <= now,
                or_(
                    AwsAuthorizationCleanupTombstoneTable.claim_expires_at.is_(None),
                    AwsAuthorizationCleanupTombstoneTable.claim_expires_at <= now,
                ),
            )
            .order_by(
                AwsAuthorizationCleanupTombstoneTable.next_reconcile_at.asc(),
                AwsAuthorizationCleanupTombstoneTable.id.asc(),
            )
            .limit(max(limit, 1))
            .with_for_update(skip_locked=True)
        )
        claimed: list[AwsAuthorizationCleanupTombstone] = []
        for row in self.session.scalars(statement):
            current = tombstone_from_row(row)
            tombstone = current.model_copy(
                update={
                    "claim_token": str(uuid4()),
                    "claim_expires_at": lease_until,
                    "updated_at": now,
                }
            )
            self._write(row, tombstone)
            claimed.append(tombstone)
        return claimed

    def finish_claim(
        self,
        claimed: AwsAuthorizationCleanupTombstone,
        updated: AwsAuthorizationCleanupTombstone,
    ) -> AwsAuthorizationCleanupTombstone | None:
        row = self._claimed_row(claimed)
        if row is None:
            return None
        finished = updated.model_copy(
            update={
                "revision": claimed.revision + 1,
                "claim_token": None,
                "claim_expires_at": None,
            }
        )
        self._write(row, finished)
        return finished

    def complete(self, claimed: AwsAuthorizationCleanupTombstone) -> bool:
        row = self._claimed_row(claimed)
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    def pending_count(self) -> int:
        return int(
            self.session.scalar(
                select(func.count()).select_from(AwsAuthorizationCleanupTombstoneTable)
            )
            or 0
        )

    def _claimed_row(
        self,
        claimed: AwsAuthorizationCleanupTombstone,
    ) -> AwsAuthorizationCleanupTombstoneTable | None:
        row = self.session.scalar(
            select(AwsAuthorizationCleanupTombstoneTable)
            .where(
                AwsAuthorizationCleanupTombstoneTable.id == claimed.id,
            )
            .with_for_update()
        )
        if (
            row is None
            or row.revision != claimed.revision
            or row.claim_token != claimed.claim_token
        ):
            return None
        return row

    def _write(
        self,
        row: AwsAuthorizationCleanupTombstoneTable,
        tombstone: AwsAuthorizationCleanupTombstone,
    ) -> None:
        tombstone = AwsAuthorizationCleanupTombstone.model_validate(dict(tombstone))
        if (
            row.id != tombstone.id
            or row.user_id != tombstone.user_id
            or row.connection_id != tombstone.connection_id
            or row.account_id != tombstone.account_id
            or row.authorization_id != tombstone.authorization.id
        ):
            raise ConflictError("AWS cleanup ownership cannot change")
        write_tombstone(row, tombstone)
        self.session.flush()
