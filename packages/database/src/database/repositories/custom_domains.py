from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.repositories.common import (
    TableRepositoryConfig,
    UserTableRepository,
)
from database.tables.custom_domains import CustomDomainTable
from shared.custom_domains import CustomDomain, CustomDomainPhase
from shared.errors import ConflictError
from shared.timestamps import utc_now
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(slots=True)
class CustomDomainRepository:
    session: Session

    @property
    def records(self) -> UserTableRepository[CustomDomain]:
        return UserTableRepository(
            self.session,
            TableRepositoryConfig(CustomDomainTable, CustomDomain),
        )

    def create(self, domain: CustomDomain, *, user_id: str) -> CustomDomain:
        try:
            return self.records.upsert(
                domain,
                user_id=user_id,
                status=domain.phase.value,
            )
        except IntegrityError as exc:
            raise ConflictError(f"domain is already registered: {domain.hostname}") from exc

    def upsert(self, domain: CustomDomain, *, user_id: str) -> CustomDomain:
        return self.records.upsert(
            domain,
            user_id=user_id,
            status=domain.phase.value,
        )

    def get(self, domain_id: str, *, user_id: str) -> CustomDomain | None:
        return self.records.get(domain_id, user_id=user_id)

    def list(self, *, user_id: str) -> list[CustomDomain]:
        return [
            domain for domain in self.records.list(user_id=user_id) if domain.deleted_at is None
        ]

    def get_by_hostname(self, hostname: str, *, user_id: str) -> CustomDomain | None:
        row = self.session.execute(
            select(CustomDomainTable)
            .where(CustomDomainTable.user_id == user_id)
            .where(CustomDomainTable.hostname == hostname)
            .where(CustomDomainTable.deleted_at.is_(None))
            .limit(1)
        ).scalar_one_or_none()
        return CustomDomain.model_validate(row.payload) if row is not None else None

    def covering(self, hostname: str, *, user_id: str) -> CustomDomain | None:
        """The account's registration a concrete hostname may be served under.

        Scoped to one account on purpose: this answers whether *this* owner may claim
        the name, so another owner's registration must not satisfy it. Any workspace
        the owner belongs to satisfies it, which is what makes one registration serve
        all of them.
        """
        candidates = self.session.execute(
            select(CustomDomainTable)
            .where(CustomDomainTable.user_id == user_id)
            .where(CustomDomainTable.deleted_at.is_(None))
        ).scalars()
        for row in candidates:
            domain = CustomDomain.model_validate(row.payload)
            if domain.covers(hostname):
                return domain
        return None

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
        return [CustomDomain.model_validate(row.payload) for row in rows]

    def soft_delete(self, domain: CustomDomain, *, user_id: str) -> CustomDomain:
        now = utc_now()
        return self.upsert(
            domain.model_copy(update={"deleted_at": now, "updated_at": now}),
            user_id=user_id,
        )


__all__ = ["CustomDomainRepository"]
