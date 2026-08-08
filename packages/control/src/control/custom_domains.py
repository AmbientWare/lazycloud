from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from database.repositories.apps import DeploymentResourceRepository
from database.repositories.custom_domains import CustomDomainRepository
from database.repositories.identity import WorkspaceMemberRepository
from shared.custom_domains import (
    CustomDomain,
    CustomDomainErrorCode,
    CustomDomainPhase,
    CustomDomainProvider,
    DnsRecord,
    ProviderCustomHostname,
    normalize_registrable_domain,
)
from shared.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    UpstreamUnavailableError,
)
from shared.timestamps import utc_now

from control.context import ControlContext

RECHECK_INTERVAL = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class CustomDomainService:
    """Registering, verifying, and retiring the domains an account owns.

    The provider is the authority on whether a hostname is serving; this service is
    the authority on whether the account asked for it. Keeping those apart is what
    lets a verification take as long as DNS takes without any request waiting on it.

    Registration is per account rather than per workspace because DNS control was
    proven once by whoever owns the domain. Which deployment serves the hostname
    stays a separate decision, made in whichever workspace that deployment lives.
    """

    context: ControlContext
    provider_factory: Callable[[], CustomDomainProvider]
    """Built per call rather than held.

    A deployment without provider credentials still starts and still serves every
    platform hostname; only the operations that genuinely need the edge fail, and
    they fail saying which configuration is missing. Holding a provider built at
    startup would have forced the opposite trade: refuse to boot, or quietly do
    nothing.
    """

    platform_base_domain: str

    @property
    def provider(self) -> CustomDomainProvider:
        try:
            return self.provider_factory()
        except ValueError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

    def register(self, domain: str, *, user_id: str) -> CustomDomain:
        try:
            hostname = normalize_registrable_domain(domain)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        self._reject_platform_domain(hostname)
        with self.context.database.session() as session:
            existing = CustomDomainRepository(session).get_by_hostname(
                hostname,
                user_id=user_id,
            )
            if existing is not None:
                return existing

        # Outside the transaction: the provider call is a network round trip, and a
        # session held across it holds a row lock for as long as the edge takes.
        state = self.provider.create_hostname(hostname)
        now = utc_now()
        record = CustomDomain(
            id=str(uuid4()),
            user_id=user_id,
            hostname=hostname,
            phase=state.phase,
            provider_hostname_id=state.provider_hostname_id,
            required_records=state.required_records,
            error_code=state.error_code,
            error_message=state.error_message,
            last_checked_at=now,
            created_at=now,
            updated_at=now,
        )
        with self.context.database.session() as session:
            try:
                return CustomDomainRepository(session).create(record, user_id=user_id)
            except ConflictError:
                # Another account won the name between the check and the write. The
                # provider hostname we just made is ours to clean up, not theirs.
                self.provider.delete_hostname(state.provider_hostname_id)
                raise

    def list(self, *, user_id: str) -> list[CustomDomain]:
        with self.context.database.session() as session:
            return CustomDomainRepository(session).list(user_id=user_id)

    def get(self, hostname: str, *, user_id: str) -> CustomDomain:
        with self.context.database.session() as session:
            found = CustomDomainRepository(session).get_by_hostname(
                hostname.strip().lower(),
                user_id=user_id,
            )
        if found is None:
            raise NotFoundError(f"domain is not registered: {hostname}")
        return found

    def remove(self, hostname: str, *, user_id: str) -> None:
        """Retire a registration, refusing while a deployment still serves under it.

        The only destructive step in this feature: it discards a certificate and
        takes every hostname under the domain offline. Deployments are checked first
        so it cannot happen as a surprise consequence of tidying up.
        """

        domain = self.get(hostname, user_id=user_id)
        with self.context.database.session() as session:
            workspace_ids = WorkspaceMemberRepository(session).owned_workspace_ids(user_id)
            # Every workspace the account owns, because the registration serves all of
            # them: a deployment in one would go dark if another workspace's tidy-up
            # retired the domain out from under it.
            claimants = {
                name
                for workspace_id in workspace_ids
                for name in DeploymentResourceRepository(session).hostnames_claimed_under(
                    workspace_id=workspace_id,
                )
            }
        serving = sorted(name for name in claimants if domain.covers(name))
        if serving:
            raise ConflictError(
                f"{domain.hostname} still serves {', '.join(serving)}; "
                f"remove the domain from those deployments first"
            )
        if domain.provider_hostname_id:
            self.provider.delete_hostname(domain.provider_hostname_id)
        with self.context.database.session() as session:
            CustomDomainRepository(session).soft_delete(
                domain,
                user_id=domain.user_id,
            )

    def refresh(self, domain: CustomDomain) -> CustomDomain:
        """Re-read one registration from the provider and record what it says."""

        if not domain.provider_hostname_id:
            return self._settle(domain, phase=CustomDomainPhase.ActionRequired)
        state = self.provider.get_hostname(domain.provider_hostname_id)
        if state is None:
            return self._settle(
                domain,
                phase=CustomDomainPhase.ActionRequired,
                error_code=CustomDomainErrorCode.HostnameRejected,
                error_message=(
                    "this domain is no longer registered with the certificate provider; "
                    "remove it here and add it again"
                ),
            )
        return self._apply(domain, state)

    def reconcile_due(self, *, now: datetime | None = None, limit: int = 50) -> int:
        """Advance every registration still waiting on the provider.

        Returns how many were re-read, so a caller polling this can tell a quiet
        cycle from a stalled one.
        """

        moment = now or utc_now()
        with self.context.database.session() as session:
            due = CustomDomainRepository(session).due_for_check(
                before=moment - RECHECK_INTERVAL,
                limit=limit,
            )
        for domain in due:
            self.refresh(domain)
        return len(due)

    def _apply(self, domain: CustomDomain, state: ProviderCustomHostname) -> CustomDomain:
        return self._settle(
            domain,
            phase=state.phase,
            required_records=state.required_records,
            error_code=state.error_code,
            error_message=state.error_message,
        )

    def _settle(
        self,
        domain: CustomDomain,
        *,
        phase: CustomDomainPhase,
        required_records: tuple[DnsRecord, ...] | None = None,
        error_code: CustomDomainErrorCode | None = None,
        error_message: str | None = None,
    ) -> CustomDomain:
        now = utc_now()
        updated = domain.model_copy(
            update={
                "phase": phase,
                "required_records": (
                    domain.required_records if required_records is None else required_records
                ),
                "error_code": error_code,
                "error_message": error_message,
                # Stamped once, when it first served: a later re-read that still says
                # ready should not keep moving the moment it became true.
                "verified_at": (
                    domain.verified_at or now
                    if phase is CustomDomainPhase.Ready
                    else domain.verified_at
                ),
                "last_checked_at": now,
                "updated_at": now,
            }
        )
        with self.context.database.session() as session:
            return CustomDomainRepository(session).upsert(
                updated,
                user_id=domain.user_id,
            )

    def _reject_platform_domain(self, hostname: str) -> None:
        base = self.platform_base_domain
        bare = hostname.removeprefix("*.")
        if base and (bare == base or bare.endswith(f".{base}")):
            raise InvalidInputError(
                f"{base} is this platform's own domain; every deployment already has a "
                f"hostname under it"
            )


__all__ = ["RECHECK_INTERVAL", "CustomDomainService"]
