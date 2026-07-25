"""Fake node agent driving the real ``ProviderNodeEnrollmentService``.

This is the in-process stand-in for the on-machine bootstrap agent: it builds
the exact production phase, enrollment, and bootstrap-failure requests and
submits them to the real service composition, so every durable transition it
causes flows through the production owners. Like the real userdata it reports
``booting`` then ``joining`` before enrolling; a failing bootstrap reports
``booting`` and then its failure reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from gateway.http import JoinAgentResponse
from gateway.provider_enrollment import ProviderNodeEnrollmentService
from provider_fake.identity import FakeStsHttpClient, fake_presigned_proof_url
from shared.compute_enrollment import MachineBootstrapFailureReason, MachineBootstrapPhase
from shared.http.provider_nodes import (
    ProviderNodeBootstrapFailureRequest,
    ProviderNodeBootstrapFailureResponse,
    ProviderNodeBootstrapPhaseRequest,
    ProviderNodeCapacity,
    ProviderNodeEnrollmentRequest,
)
from shared.provider_config import ProviderKind


class FakeNodeAgentAction(StrEnum):
    """Scripted behavior for one fake node."""

    Succeed = "succeed"
    Fail = "fail"
    Silent = "silent"


@dataclass(slots=True)
class FakeNodeAgent:
    """One fake machine's bootstrap agent bound to a provider instance."""

    enrollment: ProviderNodeEnrollmentService
    pool_id: str
    region: str
    instance_id: str
    sts_client: FakeStsHttpClient

    def run(
        self,
        action: FakeNodeAgentAction,
        *,
        reason: MachineBootstrapFailureReason = (
            MachineBootstrapFailureReason.AgentEnrollmentFailed
        ),
    ) -> None:
        if action is FakeNodeAgentAction.Succeed:
            self.report_phase(MachineBootstrapPhase.Booting)
            self.report_phase(MachineBootstrapPhase.Joining)
            self.enroll()
        elif action is FakeNodeAgentAction.Fail:
            self.report_phase(MachineBootstrapPhase.Booting)
            self.report_failure(reason)
        # Silent: do nothing and let the bootstrap phase deadlines fire.

    def report_phase(self, phase: MachineBootstrapPhase) -> ProviderNodeBootstrapFailureResponse:
        return self.enrollment.record_phase(
            ProviderNodeBootstrapPhaseRequest(
                enrollment_request_id=self.pool_id,
                provider=ProviderKind.Aws,
                region=self.region,
                provider_instance_id=self.instance_id,
                identity_proof_url=self._proof_url(),
                phase=phase,
            )
        )

    def enroll(self) -> JoinAgentResponse:
        return self.enrollment.enroll(
            ProviderNodeEnrollmentRequest(
                enrollment_request_id=self.pool_id,
                provider=ProviderKind.Aws,
                region=self.region,
                provider_instance_id=self.instance_id,
                identity_proof_url=self._proof_url(),
                machine_fingerprint=f"fake-{self.instance_id}",
                hostname=f"fake-{self.instance_id}",
                os="linux",
                arch="amd64",
                executor="runc",
                capacity=ProviderNodeCapacity(
                    cpu_count=4,
                    cpu_millicores=4_000,
                    memory_mb=32 * 1024,
                ),
            )
        )

    def report_failure(
        self,
        reason: MachineBootstrapFailureReason,
    ) -> ProviderNodeBootstrapFailureResponse:
        return self.enrollment.report_failure(
            ProviderNodeBootstrapFailureRequest(
                enrollment_request_id=self.pool_id,
                provider=ProviderKind.Aws,
                region=self.region,
                provider_instance_id=self.instance_id,
                identity_proof_url=self._proof_url(),
                failure_reason=reason,
            )
        )

    def _proof_url(self) -> str:
        self.sts_client.current_instance_id = self.instance_id
        return fake_presigned_proof_url(self.region, self.instance_id)


__all__ = ["FakeNodeAgent", "FakeNodeAgentAction"]
