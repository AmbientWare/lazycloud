from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.mappers.custom_domains import custom_domain_from_table, write_custom_domain
from database.repositories.identity import UserRepository
from database.tables.custom_domains import CustomDomainTable
from shared.custom_domains import CustomDomain, CustomDomainPhase
from shared.errors import ConflictError, NotFoundError
from shared.timestamps import utc_now
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(slots=True)
class CustomDomainRepository:
    session: Session

    def count_for_user(self, user_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count(CustomDomainTable.id)).where(
                    CustomDomainTable.user_id == user_id,
                    CustomDomainTable.deleted_at.is_(None),
                )
            )
            or 0
        )

    def create(self, domain: CustomDomain, *, user_id: str) -> CustomDomain:
        if domain.user_id != user_id:
            raise NotFoundError("domain does not belong to the account")
        UserRepository(self.session).lock_active(user_id)
        row = CustomDomainTable(id=domain.id, user_id=user_id, created_at=domain.created_at)
        write_custom_domain(row, domain)
        try:
            self.session.add(row)
            self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"domain is already registered: {domain.hostname}") from exc
        return custom_domain_from_table(row)

    def upsert(self, domain: CustomDomain, *, user_id: str) -> CustomDomain:
        UserRepository(self.session).lock_active(user_id)
        row = self.session.scalar(
            select(CustomDomainTable).where(
                CustomDomainTable.id == domain.id,
                CustomDomainTable.user_id == user_id,
            )
        )
        if row is None or domain.user_id != user_id:
            raise NotFoundError("domain does not belong to the account")
        write_custom_domain(row, domain)
        self.session.flush()
        return custom_domain_from_table(row)

    def list(self, *, user_id: str) -> list[CustomDomain]:
        rows = self.session.scalars(
            select(CustomDomainTable)
            .where(CustomDomainTable.user_id == user_id, CustomDomainTable.deleted_at.is_(None))
            .order_by(CustomDomainTable.created_at.desc(), CustomDomainTable.id)
        )
        return [custom_domain_from_table(row) for row in rows]

    def get_by_hostname(self, hostname: str, *, user_id: str) -> CustomDomain | None:
        row = self.session.execute(
            select(CustomDomainTable)
            .where(CustomDomainTable.user_id == user_id)
            .where(CustomDomainTable.hostname == hostname)
            .where(CustomDomainTable.deleted_at.is_(None))
            .limit(1)
        ).scalar_one_or_none()
        return custom_domain_from_table(row) if row is not None else None

    def due_for_check(self, *, before: datetime, limit: int = 50) -> list[CustomDomain]:
        """Registrations the reconciler should re-read from the provider.

        System listing across accounts: verification is the provider's answer about a
        global namespace, so it is not any one owner's query.
        """
        unsettled = (
            CustomDomainPhase.AwaitingVerification.value,
            CustomDomainPhase.Validating.value,
        )
        rows = self.session.execute(
            select(CustomDomainTable)
            .where(CustomDomainTable.deleted_at.is_(None))
            .where(CustomDomainTable.phase.in_(unsettled))
            .where(
                CustomDomainTable.last_checked_at.is_(None)
                | (CustomDomainTable.last_checked_at < before)
            )
            .order_by(CustomDomainTable.last_checked_at.asc().nulls_first())
            .limit(limit)
        ).scalars()
        return [custom_domain_from_table(row) for row in rows]

    def soft_delete(self, domain: CustomDomain, *, user_id: str) -> CustomDomain:
        now = utc_now()
        return self.upsert(
            domain.model_copy(update={"deleted_at": now, "updated_at": now}),
            user_id=user_id,
        )


__all__ = ["CustomDomainRepository"]
