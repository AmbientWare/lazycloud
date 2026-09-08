from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from secrets import compare_digest, token_urlsafe
from typing import Protocol
from uuid import uuid4

from database.repositories.compute import ComputeMachineEnrollmentRepository, ComputeUnitRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.provider_launches import ProviderNodeLaunchRepository
from database.tables.provider_launches import ProviderNodeLaunchTable
from database.types import DatabaseSession
from pydantic import SecretStr
from shared.compute_enrollment import ComputeMachineEnrollmentStatus
from shared.compute_policy import ENDED_UNIT_PHASES, ComputeUnitRecord
from shared.errors import ConflictError, InvalidInputError, UpstreamUnavailableError
from shared.http.provider_nodes import ProviderNodeIdentityRequest
from shared.provider_config import ProviderKind
from shared.timestamps import to_utc, utc_now

from compute.agent_control import hash_compute_token, hash_machine_fingerprint
from compute.providers import ProviderUnitRequest
from database import DatabaseClient

_BOOTSTRAP_TTL = timedelta(minutes=20)


class ProviderNodeSecretCipher(Protocol):
    def encrypt(self, name: str, plaintext: str) -> str: ...

    def decrypt(self, name: str, ciphertext: str) -> str: ...


@dataclass(frozen=True, slots=True)
class ProviderNodeLaunchCredential:
    launch_id: str
    server_name: str
    bootstrap_token: SecretStr
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderNodeLaunchStatus:
    launch_id: str
    provider_instance_id: str | None
    expires_at: datetime
    redeemed: bool
    revoked: bool
    creation_attempted: bool
    server_name: str
    generation: int
    provider_operation_id: str | None
    provider_resource_id: str | None


class ProviderNodeLaunchCredentials(Protocol):
    def prepare(
        self, request: ProviderUnitRequest, server_name: str
    ) -> ProviderNodeLaunchCredential: ...

    def for_server(
        self, request: ProviderUnitRequest, server_name: str
    ) -> ProviderNodeLaunchStatus | None: ...

    def for_unit(self, request: ProviderUnitRequest) -> tuple[ProviderNodeLaunchStatus, ...]: ...

    def for_instance(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
    ) -> ProviderNodeLaunchStatus | None: ...

    def bind(
        self,
        launch_id: str,
        *,
        provider_ref: str,
        provider_instance_id: str,
        unit_id: str,
        server_name: str,
        region: str,
        generation: int,
        provider_resource_id: str | None = None,
    ) -> None: ...

    def revoke(self, launch_id: str) -> None: ...

    def mark_creation_attempt(self, launch_id: str) -> bool: ...

    def record_creation_operation(self, launch_id: str, operation_id: str) -> None: ...


@dataclass(slots=True)
class ProviderNodeEnrollmentLease:
    _record: ProviderNodeLaunchTable

    @property
    def enrolled(self) -> bool:
        return self._record.enrolled_at is not None

    def complete(self) -> None:
        self._record.enrolled_at = self._record.enrolled_at or utc_now()


@dataclass(slots=True)
class ProviderNodeLaunchService:
    database: DatabaseClient
    cipher_for_workspace: Callable[[str], ProviderNodeSecretCipher]

    def prepare(
        self, request: ProviderUnitRequest, server_name: str
    ) -> ProviderNodeLaunchCredential:
        now = utc_now()
        cipher = self.cipher_for_workspace(request.workspace_id)
        with self.database.session() as session:
            WorkspaceRepository(session).lock_active_owner(request.workspace_id)
            repository = ProviderNodeLaunchRepository(session)
            repository.lock_unit(request.unit_id)
            pool = ComputeUnitRepository(session).get(request.unit_id)
            if (
                pool is None
                or pool.workspace_id != request.workspace_id
                or pool.provider_ref != request.provider_ref
                or pool.region != request.offer.region
                or pool.generation != request.generation
                or pool.phase in ENDED_UNIT_PHASES
                or not pool.platform_fleet
                or pool.provider_ref.partition(":")[0]
                not in {ProviderKind.Hetzner, ProviderKind.Hyperstack, ProviderKind.Ovh}
            ):
                raise InvalidInputError("provider launch does not match an active platform unit")
            launch = repository.active_slot(pool.id, server_name)
            # The workspace key is stored in this database too. This encryption
            # prevents plaintext exposure and binds the ciphertext to its launch;
            # it does not defend against an attacker who can read the whole DB.
            if launch is None:
                launch_id = str(uuid4())
                token = token_urlsafe(32)
                launch = ProviderNodeLaunchTable(
                    id=launch_id,
                    workspace_id=pool.workspace_id,
                    unit_id=pool.id,
                    provider_ref=pool.provider_ref,
                    region=pool.region,
                    generation=pool.generation,
                    server_name=server_name,
                    bootstrap_token_hash=hash_compute_token(token),
                    bootstrap_token_ciphertext=cipher.encrypt(_cipher_name(launch_id), token),
                    created_at=now,
                    expires_at=now + _BOOTSTRAP_TTL,
                )
                repository.save(launch)
            else:
                if (
                    launch.redeemed_at is not None
                    or to_utc(launch.expires_at) <= now
                    or launch.bootstrap_token_ciphertext is None
                    or launch.generation != request.generation
                ):
                    raise ConflictError("provider launch must be retired before slot replacement")
                token = cipher.decrypt(_cipher_name(launch.id), launch.bootstrap_token_ciphertext)
            return ProviderNodeLaunchCredential(
                launch.id, launch.server_name, SecretStr(token), to_utc(launch.expires_at)
            )

    def for_server(
        self, request: ProviderUnitRequest, server_name: str
    ) -> ProviderNodeLaunchStatus | None:
        with self.database.session() as session:
            launch = ProviderNodeLaunchRepository(session).active_slot(request.unit_id, server_name)
            if launch is None:
                return None
            if launch.provider_ref != request.provider_ref:
                raise InvalidInputError("provider launch belongs to another binding")
            return _status(launch)

    def for_unit(self, request: ProviderUnitRequest) -> tuple[ProviderNodeLaunchStatus, ...]:
        with self.database.session() as session:
            launches = ProviderNodeLaunchRepository(session).active_for_unit(request.unit_id)
            if any(
                launch.workspace_id != request.workspace_id
                or launch.provider_ref != request.provider_ref
                for launch in launches
            ):
                raise InvalidInputError("provider launches belong to another binding")
            return tuple(_status(launch) for launch in launches)

    def mark_creation_attempt(self, launch_id: str) -> bool:
        now = utc_now()
        with self.database.session() as session:
            repository = ProviderNodeLaunchRepository(session)
            pending = repository.get(launch_id)
            if pending is None:
                raise InvalidInputError("provider launch creation authorization is unavailable")
            WorkspaceRepository(session).lock_active_owner(pending.workspace_id)
            repository.lock_unit(pending.unit_id)
            pool = ComputeUnitRepository(session).get(pending.unit_id)
            launch = repository.get(launch_id, for_update=True)
            if (
                launch is None
                or pool is None
                or pool.phase in ENDED_UNIT_PHASES
                or pool.generation != launch.generation
                or pool.provider_ref != launch.provider_ref
                or pool.region != launch.region
                or pool.workspace_id != launch.workspace_id
                or launch.revoked_at is not None
                or launch.redeemed_at is not None
                or to_utc(launch.expires_at) <= now
            ):
                raise InvalidInputError("provider launch creation authorization is unavailable")
            if launch.creation_attempted_at is not None or launch.provider_instance_id is not None:
                return False
            launch.creation_attempted_at = now
            repository.save(launch)
            return True

    def for_instance(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
    ) -> ProviderNodeLaunchStatus | None:
        with self.database.session() as session:
            launch = ProviderNodeLaunchRepository(session).for_instance(
                request.unit_id,
                request.provider_ref,
                provider_instance_id,
            )
            if launch is None:
                return None
            if launch.workspace_id != request.workspace_id:
                raise InvalidInputError("provider launch belongs to another workspace")
            return _status(launch)

    def record_creation_operation(self, launch_id: str, operation_id: str) -> None:
        if not operation_id or len(operation_id) > 128:
            raise InvalidInputError("provider creation requires an operation identity")
        with self.database.session() as session:
            repository = ProviderNodeLaunchRepository(session)
            launch = repository.get(launch_id, for_update=True)
            if (
                launch is None
                or launch.revoked_at is not None
                or launch.creation_attempted_at is None
            ):
                raise InvalidInputError("provider launch creation is unavailable")
            if launch.provider_operation_id not in {None, operation_id}:
                raise ConflictError("provider launch already has another creation operation")
            launch.provider_operation_id = operation_id
            repository.save(launch)

    def bind(
        self,
        launch_id: str,
        *,
        provider_ref: str,
        provider_instance_id: str,
        unit_id: str,
        server_name: str,
        region: str,
        generation: int,
        provider_resource_id: str | None = None,
    ) -> None:
        if provider_resource_id is not None and (
            not provider_resource_id or len(provider_resource_id) > 128
        ):
            raise InvalidInputError("provider launch requires a valid resource identity")
        with self.database.session() as session:
            launch = ProviderNodeLaunchRepository(session).get(launch_id, for_update=True)
            if (
                launch is None
                or launch.revoked_at is not None
                or launch.provider_ref != provider_ref
                or launch.unit_id != unit_id
                or launch.server_name != server_name
                or launch.region != region
                or launch.generation != generation
            ):
                raise InvalidInputError("provider launch binding is unavailable")
            if launch.provider_instance_id not in {None, provider_instance_id}:
                raise ConflictError("provider launch is already bound to another instance")
            if provider_resource_id is not None and launch.provider_resource_id not in {
                None,
                provider_resource_id,
            }:
                raise ConflictError("provider launch is already bound to another resource")
            if not provider_instance_id:
                raise InvalidInputError("provider launch requires an instance identity")
            launch.provider_instance_id = provider_instance_id
            if provider_resource_id is not None:
                launch.provider_resource_id = provider_resource_id
            ProviderNodeLaunchRepository(session).save(launch)

    def revoke(self, launch_id: str) -> None:
        with self.database.session() as session:
            launch = ProviderNodeLaunchRepository(session).get(launch_id, for_update=True)
            if launch is not None:
                launch.revoked_at = launch.revoked_at or utc_now()
                launch.bootstrap_token_ciphertext = None

    def authorize(self, request: ProviderNodeIdentityRequest, pool: ComputeUnitRecord) -> None:
        with self.database.session() as session:
            launch = _require_launch(
                ProviderNodeLaunchRepository(session).get(request.launch_id, for_update=True),
                request,
                pool,
            )
            node_hash = hash_compute_token(request.node_agent_token)
            if compare_digest(launch.bootstrap_token_hash, node_hash):
                raise InvalidInputError("node credential must differ from its bootstrap credential")
            enrollment = ComputeMachineEnrollmentRepository(session).by_credential_hash(node_hash)
            if launch.redeemed_at is None:
                if not request.bootstrap_token or not compare_digest(
                    launch.bootstrap_token_hash, hash_compute_token(request.bootstrap_token)
                ):
                    raise InvalidInputError("provider bootstrap credential is invalid")
                launch.redeemed_at = utc_now()
                launch.node_token_hash = node_hash
                launch.bootstrap_token_ciphertext = None
            elif launch.node_token_hash is None or not compare_digest(
                launch.node_token_hash, node_hash
            ):
                raise InvalidInputError("provider node credential is invalid")
            if enrollment is not None and (
                enrollment.status is not ComputeMachineEnrollmentStatus.Active
                or enrollment.capacity_owner_id != pool.capacity_owner_id
            ):
                raise InvalidInputError("provider node credential is unavailable for this launch")
            if launch.enrolled_at is not None and enrollment is None:
                raise InvalidInputError("provider node enrollment no longer exists")
            ProviderNodeLaunchRepository(session).save(launch)

    def lock_enrollment(
        self,
        session: DatabaseSession,
        request: ProviderNodeIdentityRequest,
        pool: ComputeUnitRecord,
        *,
        machine_fingerprint: str,
    ) -> ProviderNodeEnrollmentLease:
        launch = _require_launch(
            ProviderNodeLaunchRepository(session).get(request.launch_id, for_update=True),
            request,
            pool,
        )
        if launch.node_token_hash is None or not compare_digest(
            launch.node_token_hash, hash_compute_token(request.node_agent_token)
        ):
            raise InvalidInputError("provider node credential is invalid")
        fingerprint = hash_machine_fingerprint(machine_fingerprint)
        if launch.fingerprint_hash not in {None, fingerprint}:
            raise InvalidInputError("provider launch cannot enroll another machine")
        launch.fingerprint_hash = fingerprint
        return ProviderNodeEnrollmentLease(launch)


def _require_launch(
    launch: ProviderNodeLaunchTable | None,
    request: ProviderNodeIdentityRequest,
    pool: ComputeUnitRecord,
) -> ProviderNodeLaunchTable:
    if (
        launch is None
        or launch.revoked_at is not None
        or (launch.redeemed_at is None and to_utc(launch.expires_at) <= utc_now())
        or launch.unit_id != pool.id
        or launch.provider_ref != pool.provider_ref
        or launch.region != request.region
    ):
        raise InvalidInputError("provider launch authorization is unavailable or expired")
    if launch.provider_instance_id is None:
        raise UpstreamUnavailableError("provider launch identity binding is still pending")
    if launch.provider_instance_id != request.provider_instance_id:
        raise InvalidInputError("provider launch does not authorize this instance")
    return launch


def _cipher_name(launch_id: str) -> str:
    return f"provider-node-bootstrap:{launch_id}"


def _status(launch: ProviderNodeLaunchTable) -> ProviderNodeLaunchStatus:
    return ProviderNodeLaunchStatus(
        launch_id=launch.id,
        provider_instance_id=launch.provider_instance_id,
        expires_at=to_utc(launch.expires_at),
        redeemed=launch.redeemed_at is not None,
        revoked=launch.revoked_at is not None,
        creation_attempted=launch.creation_attempted_at is not None,
        server_name=launch.server_name,
        generation=launch.generation,
        provider_operation_id=launch.provider_operation_id,
        provider_resource_id=launch.provider_resource_id,
    )
