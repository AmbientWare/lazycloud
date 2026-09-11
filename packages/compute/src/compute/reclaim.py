"""Typed reclaim windows for managed provider machine reconciliation.

Bootstrap-phase reclaim: a launched machine must progress through its
bootstrap phases. Each pre-ready phase carries its own deadline measured from
the last observed phase transition; a machine stuck past its phase deadline is
terminated at its owning provider instead of leaking and accruing hourly
renewals until credits are exhausted.

Repeated bootstrap failures are additionally bounded per pool: once a machine
record exhausts ``max_launch_attempts`` consecutive launches, the owning pooled
capacity is marked degraded instead of relaunching forever.

All windows are typed settings with production defaults and optional
per-provider overrides for providers whose boot or join behavior does not fit
the default bound.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.compute_enrollment import MachineBootstrapPhase
from shared.contracts import ContractModel

DEFAULT_BOOTSTRAP_PHASE_DEADLINE_SECONDS: dict[str, int] = {
    MachineBootstrapPhase.Requested.value: 300,
    MachineBootstrapPhase.Provisioning.value: 300,
    MachineBootstrapPhase.Booting.value: 300,
    MachineBootstrapPhase.Joining.value: 300,
    MachineBootstrapPhase.Failed.value: 300,
}
"""Machines stuck in one bootstrap phase are reclaimed after 5 minutes.

Each phase deadline restarts on an observed phase transition, so a healthy but
slow bootstrap gets the full window per phase while a machine that dies in any
single phase is bounded to minutes, not a billed hour.

`Failed` is bounded for the same reason and on the same clock. Without it a
machine that reported why it failed ran and billed until someone noticed, while
one that died silently was reclaimed in five minutes — so the boot path that
reported nothing cost less than the one that reported everything. The window is
what a machine gets to be looked at, not a licence to keep it.
"""

DEFAULT_MAX_LAUNCH_ATTEMPTS = 3
"""Pooled capacity stops relaunching after 3 consecutive failed bootstraps.

A systematic bootstrap failure (bad image, broken network policy) would
otherwise terminate and relaunch billable machines forever; after the bound the
pool is marked degraded until capacity is explicitly changed.
"""

DEFAULT_BOOTSTRAP_FAILURE_OBSERVATIONS = 2
"""Consecutive non-serving observations before a bootstrap deadline reclaims.

The deadline says a machine is late; this says we looked twice and it was still
late. One sample is a fact about the instant it was taken, and the things that
make it wrong — a hot record between writes, a heartbeat mid-flight — clear
within a pass.
"""

DEFAULT_SERVICE_LOSS_OBSERVATIONS = 5
DEFAULT_SERVICE_LOSS_WINDOW_SECONDS = 600
"""What it takes to believe a machine that worked has stopped.

Both, not either. A window alone reduces to one sample taken late: nothing looks
for ten minutes, then the first look after them terminates. A count alone is
satisfied by a burst of passes in a few seconds. The count is "we looked
repeatedly", the window is "over real time", and a machine that served has
earned the longer of the two.
"""

DEFAULT_LIVE_CONTAINER_RECLAIM_GRACE_SECONDS = 1800
"""How long a machine's own containers may hold it against reclaim.

A machine still running work is not one to terminate on a readiness signal, but
the claim has to end: container rows outlive the worker that owned them when
nothing ticks to settle them, and an unbounded veto is a meter that never stops.
"""

_DEADLINE_PHASES = frozenset(DEFAULT_BOOTSTRAP_PHASE_DEADLINE_SECONDS)


class ComputeReclaimPolicy(ContractModel):
    """Reclaim windows applied by ``ComputeService`` reconciliation.

    ``provider_*`` maps override the default per configured provider name.
    """

    bootstrap_phase_deadline_seconds: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_BOOTSTRAP_PHASE_DEADLINE_SECONDS)
    )
    max_launch_attempts: int = Field(default=DEFAULT_MAX_LAUNCH_ATTEMPTS, gt=0)
    bootstrap_failure_observations: int = Field(
        default=DEFAULT_BOOTSTRAP_FAILURE_OBSERVATIONS, gt=0
    )
    service_loss_observations: int = Field(default=DEFAULT_SERVICE_LOSS_OBSERVATIONS, gt=0)
    service_loss_window_seconds: int = Field(default=DEFAULT_SERVICE_LOSS_WINDOW_SECONDS, gt=0)
    live_container_reclaim_grace_seconds: int = Field(
        default=DEFAULT_LIVE_CONTAINER_RECLAIM_GRACE_SECONDS, gt=0
    )
    provider_bootstrap_phase_deadline_seconds: dict[str, dict[str, int]] = Field(
        default_factory=dict
    )
    provider_max_launch_attempts: dict[str, int] = Field(default_factory=dict)

    @property
    def service_loss_window(self) -> timedelta:
        return timedelta(seconds=self.service_loss_window_seconds)

    @property
    def live_container_reclaim_grace(self) -> timedelta:
        return timedelta(seconds=self.live_container_reclaim_grace_seconds)

    def phase_deadline_for(
        self,
        provider: str,
        phase: MachineBootstrapPhase,
    ) -> timedelta | None:
        """Return the reclaim deadline for one bootstrap phase, or None for no bound."""

        overrides = self.provider_bootstrap_phase_deadline_seconds.get(provider, {})
        seconds = overrides.get(phase.value, self.bootstrap_phase_deadline_seconds.get(phase.value))
        if seconds is None:
            return None
        return timedelta(seconds=seconds)

    def max_launch_attempts_for(self, provider: str) -> int:
        return self.provider_max_launch_attempts.get(provider, self.max_launch_attempts)


def _validated_phase_deadlines(value: dict[str, int]) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for raw_phase, seconds in value.items():
        phase = raw_phase.strip()
        if phase not in _DEADLINE_PHASES:
            raise ValueError(
                "compute reclaim bootstrap phase deadlines accept only "
                + ", ".join(sorted(_DEADLINE_PHASES))
            )
        if seconds <= 0:
            raise ValueError("compute reclaim bootstrap phase deadline seconds must be positive")
        normalized[phase] = seconds
    return normalized


class ComputeReclaimSettings(BaseSettings):
    """Environment tuning for the reclaim windows, parsed at composition roots.

    ``provider_*`` map values are JSON objects keyed by configured provider
    name, matching the map-valued settings convention used elsewhere (for
    example the AWS capacity AMI catalogs).
    """

    bootstrap_phase_deadline_seconds: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_BOOTSTRAP_PHASE_DEADLINE_SECONDS)
    )
    max_launch_attempts: int = Field(default=DEFAULT_MAX_LAUNCH_ATTEMPTS, gt=0)
    bootstrap_failure_observations: int = Field(
        default=DEFAULT_BOOTSTRAP_FAILURE_OBSERVATIONS, gt=0
    )
    service_loss_observations: int = Field(default=DEFAULT_SERVICE_LOSS_OBSERVATIONS, gt=0)
    service_loss_window_seconds: int = Field(default=DEFAULT_SERVICE_LOSS_WINDOW_SECONDS, gt=0)
    live_container_reclaim_grace_seconds: int = Field(
        default=DEFAULT_LIVE_CONTAINER_RECLAIM_GRACE_SECONDS, gt=0
    )
    provider_bootstrap_phase_deadline_seconds: dict[str, dict[str, int]] = Field(
        default_factory=dict
    )
    provider_max_launch_attempts: dict[str, int] = Field(default_factory=dict)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_COMPUTE_RECLAIM_",
        extra="ignore",
    )

    @field_validator("provider_max_launch_attempts")
    @classmethod
    def normalize_provider_overrides(cls, value: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for raw_provider, count in value.items():
            provider = raw_provider.strip()
            if not provider:
                raise ValueError("compute reclaim provider override keys cannot be empty")
            if count <= 0:
                raise ValueError("compute reclaim provider override values must be positive")
            normalized[provider] = count
        return normalized

    @field_validator("bootstrap_phase_deadline_seconds")
    @classmethod
    def normalize_phase_deadlines(cls, value: dict[str, int]) -> dict[str, int]:
        # Merged onto the defaults rather than replacing them. A phase absent
        # from this map has no deadline at all, so a deployment tuning one phase
        # would silently unbound the other four — machines that die in those
        # phases would then never be reclaimed and would bill until noticed.
        return {**DEFAULT_BOOTSTRAP_PHASE_DEADLINE_SECONDS, **_validated_phase_deadlines(value)}

    @field_validator("provider_bootstrap_phase_deadline_seconds")
    @classmethod
    def normalize_provider_phase_deadlines(
        cls,
        value: dict[str, dict[str, int]],
    ) -> dict[str, dict[str, int]]:
        normalized: dict[str, dict[str, int]] = {}
        for raw_provider, deadlines in value.items():
            provider = raw_provider.strip()
            if not provider:
                raise ValueError("compute reclaim provider override keys cannot be empty")
            normalized[provider] = _validated_phase_deadlines(deadlines)
        return normalized

    def to_policy(self) -> ComputeReclaimPolicy:
        return ComputeReclaimPolicy(
            bootstrap_phase_deadline_seconds=self.bootstrap_phase_deadline_seconds,
            max_launch_attempts=self.max_launch_attempts,
            bootstrap_failure_observations=self.bootstrap_failure_observations,
            service_loss_observations=self.service_loss_observations,
            service_loss_window_seconds=self.service_loss_window_seconds,
            live_container_reclaim_grace_seconds=self.live_container_reclaim_grace_seconds,
            provider_bootstrap_phase_deadline_seconds=(
                self.provider_bootstrap_phase_deadline_seconds
            ),
            provider_max_launch_attempts=self.provider_max_launch_attempts,
        )


__all__ = [
    "DEFAULT_BOOTSTRAP_FAILURE_OBSERVATIONS",
    "DEFAULT_BOOTSTRAP_PHASE_DEADLINE_SECONDS",
    "DEFAULT_LIVE_CONTAINER_RECLAIM_GRACE_SECONDS",
    "DEFAULT_MAX_LAUNCH_ATTEMPTS",
    "DEFAULT_SERVICE_LOSS_OBSERVATIONS",
    "DEFAULT_SERVICE_LOSS_WINDOW_SECONDS",
    "ComputeReclaimPolicy",
    "ComputeReclaimSettings",
]
