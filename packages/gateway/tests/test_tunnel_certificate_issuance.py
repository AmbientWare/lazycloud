from __future__ import annotations

import secrets
from pathlib import Path
from uuid import uuid4

import pytest
from compute.agent_control import hash_compute_token
from compute.state import ComputeAgentTokenState, RedisComputeStateRepository
from database.context import ServiceContext
from database.repositories.compute import (
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRepository,
)
from database.repositories.orchestration import MachineRepository
from gateway.settings import TunnelCertificateSettings
from gateway.tunnel_certificates import TunnelCertificateService
from identity.auth import AuthError
from identity.tunnel_certificates import (
    agent_identity_from_verified_certificate,
    certificate_public_key_sha256,
    create_tunnel_certificate_authority,
    service_identity_from_verified_certificate,
)
from networking.tunnel_tls import agent_certificate_request
from pydantic import SecretStr
from shared.compute_enrollment import ComputeMachineEnrollmentStatus
from shared.compute_fleet import Machine, ResourceStatus
from shared.compute_policy import MachinePool
from shared.errors import ConflictError
from shared.http.agent_identity import (
    AgentCertificateRequest,
    GatewayCertificateRequest,
    TunnelServiceRole,
)
from shared.timestamps import utc_now
from tests.real_redis import RealRedisActors
from tests.workspaces import workspace_owner_user_id


@pytest.fixture
def certificate_service(
    service_context: ServiceContext, real_redis_actors: RealRedisActors, tmp_path: Path
) -> TunnelCertificateService:
    authority = create_tunnel_certificate_authority(deployment_hostname="tunnel.example.test")
    certificate_path = tmp_path / "issuer.pem"
    key_path = tmp_path / "issuer-key.pem"
    certificate_path.write_text(authority.certificate_pem)
    key_path.write_text(authority.private_key_pem)
    key_path.chmod(0o600)
    return TunnelCertificateService.load(
        service_context.database,
        RedisComputeStateRepository(real_redis_actors.client()),
        TunnelCertificateSettings(
            hostname="tunnel.example.test",
            gateway_bootstrap_secret=SecretStr(secrets.token_urlsafe(32)),
            issuer_certificate_file=certificate_path,
            issuer_private_key_file=key_path,
        ),
    )


def test_agent_issuance_requires_current_enrollment_and_its_bound_key(
    certificate_service: TunnelCertificateService, service_context: ServiceContext, tmp_path: Path
) -> None:
    context = service_context
    token = secrets.token_urlsafe(32)
    token_hash = hash_compute_token(token)
    with context.database.session() as session:
        workspace_id = context.default_workspace_id(session)
    user_id = workspace_owner_user_id(context, workspace_id)
    machine_id, owner_id = str(uuid4()), str(uuid4())
    with context.database.session() as session:
        MachineRepository(session).upsert(
            Machine(id=machine_id, capacity_owner_id=owner_id, status=ResourceStatus.Running),
            workspace_id=workspace_id,
        )
        enrollment = ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                user_id=user_id,
                workspace_id=workspace_id,
                capacity_owner_id=owner_id,
                pool=MachinePool("lazycloud"),
                machine_id=machine_id,
                machine_fingerprint_hash=hash_compute_token(machine_id),
                credential_hash=token_hash,
                last_join_at=utc_now(),
            )
        )
    csr_pem = agent_certificate_request(tmp_path / "agent-key.pem")
    with pytest.raises(AuthError):
        certificate_service.issue_agent(
            AgentCertificateRequest(agent_token="unknown", csr_pem=csr_pem)
        )
    response = certificate_service.issue_agent(
        AgentCertificateRequest(agent_token=token, csr_pem=csr_pem)
    )
    assert response.identity.enrollment_id == enrollment.id
    assert response.identity.workspace_id == workspace_id
    assert response.tunnel_address == "tunnel.example.test:443"
    assert agent_identity_from_verified_certificate(response.certificate_pem) == response.identity
    with context.database.session() as session:
        current = ComputeMachineEnrollmentRepository(session).by_id(
            enrollment.id, workspace_id=workspace_id
        )
        assert current is not None
        assert current.tunnel_public_key_sha256 == certificate_public_key_sha256(
            response.certificate_pem
        )
    with pytest.raises(ConflictError):
        certificate_service.issue_agent(
            AgentCertificateRequest(
                agent_token=token,
                csr_pem=agent_certificate_request(tmp_path / "replacement-key.pem"),
            )
        )
    certificate_service.authority.compute_states.save_agent_token_state(
        ComputeAgentTokenState(
            token_hash=token_hash,
            workspace_id=workspace_id,
            capacity_owner_id=owner_id,
            machine_id=machine_id,
            pool=enrollment.pool,
            credential_id=enrollment.id,
            credential_generation=enrollment.credential_generation,
        )
    )
    with context.database.session() as session:
        repository = ComputeMachineEnrollmentRepository(session)
        current = repository.by_id(enrollment.id, workspace_id=workspace_id)
        assert current is not None
        repository.save(
            current.model_copy(update={"status": ComputeMachineEnrollmentStatus.Revoked})
        )
    with pytest.raises(AuthError):
        certificate_service.issue_agent(AgentCertificateRequest(agent_token=token, csr_pem=csr_pem))


def test_gateway_bootstrap_credential_cannot_issue_an_agent_or_control_plane_identity(
    certificate_service: TunnelCertificateService, tmp_path: Path
) -> None:
    csr_pem = agent_certificate_request(tmp_path / "gateway-key.pem")
    request = GatewayCertificateRequest(csr_pem=csr_pem, instance_id=str(uuid4()))
    with pytest.raises(AuthError):
        certificate_service.issue_gateway(request, authorization="Bearer invalid")
    credential = certificate_service.settings.gateway_bootstrap_secret.get_secret_value()
    response = certificate_service.issue_gateway(request, authorization=f"Bearer {credential}")
    assert response.identity.role is TunnelServiceRole.Gateway
    assert service_identity_from_verified_certificate(response.certificate_pem) == response.identity
    with pytest.raises(AuthError):
        certificate_service.issue_agent(
            AgentCertificateRequest(agent_token=credential, csr_pem=csr_pem)
        )
