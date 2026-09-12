from __future__ import annotations

from dataclasses import dataclass, field
from hmac import compare_digest

from compute.agent_control import hash_compute_token
from compute.state import RedisComputeStateRepository
from compute.tunnel_authority import AgentTunnelAuthority
from database.repositories.compute import ComputeMachineEnrollmentRepository
from identity.auth import AuthError
from identity.tunnel_certificates import TunnelCertificateIssuer, csr_public_key_sha256
from shared.compute_enrollment import ComputeMachineEnrollmentStatus
from shared.http.agent_identity import (
    AgentCertificateRequest,
    AgentCertificateResponse,
    GatewayCertificateRequest,
    ServiceCertificateResponse,
    TunnelServiceRole,
)

from database import DatabaseClient
from gateway.settings import TunnelCertificateSettings


@dataclass(frozen=True, slots=True)
class TunnelCertificateService:
    authority: AgentTunnelAuthority = field(repr=False)
    issuer: TunnelCertificateIssuer = field(repr=False)
    settings: TunnelCertificateSettings

    @classmethod
    def load(
        cls,
        database: DatabaseClient,
        compute_states: RedisComputeStateRepository,
        settings: TunnelCertificateSettings,
    ) -> TunnelCertificateService:
        return cls(
            AgentTunnelAuthority(database, compute_states),
            TunnelCertificateIssuer.load(
                settings.issuer_certificate_file, settings.issuer_private_key_file
            ),
            settings,
        )

    def issue_agent(self, request: AgentCertificateRequest) -> AgentCertificateResponse:
        token_hash = hash_compute_token(request.agent_token)
        enrollment_id, workspace_id = self._agent_scope(token_hash)
        public_key_sha256 = csr_public_key_sha256(request.csr_pem)
        identity = self.authority.bind_key(
            enrollment_id, workspace_id, token_hash, public_key_sha256
        )
        certificate = self.issuer.issue_agent(request.csr_pem, identity)
        return AgentCertificateResponse(
            **certificate.model_dump(), tunnel_address=self.settings.tunnel_address
        )

    def issue_gateway(
        self, request: GatewayCertificateRequest, *, authorization: str
    ) -> ServiceCertificateResponse:
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or not compare_digest(
            credential.encode("utf-8"),
            self.settings.gateway_bootstrap_secret.get_secret_value().encode("utf-8"),
        ):
            raise AuthError("Invalid gateway bootstrap credential")
        return self.issuer.issue_service(
            request.csr_pem,
            TunnelServiceRole.Gateway,
            request.instance_id,
            (self.settings.hostname,),
        )

    def issue_control_plane(self, csr_pem: str, instance_id: str) -> ServiceCertificateResponse:
        return self.issuer.issue_service(csr_pem, TunnelServiceRole.ControlPlane, instance_id)

    def _agent_scope(self, token_hash: str) -> tuple[str, str]:
        state = self.authority.compute_states.get_agent_token_state(token_hash)
        with self.authority.database.session() as session:
            repository = ComputeMachineEnrollmentRepository(session)
            if state is not None:
                credential = repository.credential_by_hash(token_hash)
                if (
                    credential is None
                    or credential.status is not ComputeMachineEnrollmentStatus.Active
                ):
                    raise AuthError("Invalid agent token")
                if (
                    state.token_hash == token_hash
                    and state.credential_id == credential.id
                    and state.credential_generation == credential.credential_generation
                ):
                    return state.credential_id, state.workspace_id
            enrollment = repository.by_credential_hash(token_hash)
            if enrollment is None or enrollment.status is not ComputeMachineEnrollmentStatus.Active:
                raise AuthError("Invalid agent token")
            return enrollment.id, enrollment.workspace_id


__all__ = ["TunnelCertificateService"]
