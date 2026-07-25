"""Typed reclaim windows for managed provider machine reconciliation.

The compute reconciler makes three time-based reclaim decisions:

- Stale termination: a machine the provider reports for a managed pool but that
  durable state does not expect is an orphan. It is terminated only after it has
  been continuously observed as stale for the provider's grace window.
- Launch-intent settlement: durable intent is committed before provider creation.
  If the provider id is not bound shortly afterward, reconciliation waits for
  provider identity visibility, then discovers and reclaims the abandoned launch.
- Bootstrap-phase reclaim: a launched machine must progress through its
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

DEFAULT_STALE_GRACE_SECONDS = 900
"""Orphaned provider machines are reclaimed after 15 minutes of staleness.

Long enough to cover an open multi-node launch transaction and GPU cloud-init,
short enough that leaked instances are terminated well within the hour.
"""

DEFAULT_BOOTSTRAP_PHASE_DEADLINE_SECONDS: dict[str, int] = {
    MachineBootstrapPhase.Requested.value: 300,
    MachineBootstrapPhase.Provisioning.value: 300,
    MachineBootstrapPhase.Booting.value: 300,
    MachineBootstrapPhase.Joining.value: 300,
}
"""Machines stuck in one bootstrap phase are reclaimed after 5 minutes.

Each phase deadline restarts on an observed phase transition, so a healthy but
slow bootstrap gets the full window per phase while a machine that dies in any
single phase is bounded to minutes, not a billed hour.
"""

DEFAULT_MAX_LAUNCH_ATTEMPTS = 3
"""Pooled capacity stops relaunching after 3 consecutive failed bootstraps.

A systematic bootstrap failure (bad image, broken network policy) would
otherwise terminate and relaunch billable machines forever; after the bound the
pool is marked degraded until capacity is explicitly changed.
"""

DEFAULT_LAUNCH_INTENT_SETTLE_SECONDS = 30
"""Unbound provider launches are discovered and reclaimed after 30 seconds.

This window covers normal provider inventory eventual consistency without
conflating a failed bind transaction with the much longer per-phase machine
bootstrap deadlines.
"""

_DEADLINE_PHASES = frozenset(DEFAULT_BOOTSTRAP_PHASE_DEADLINE_SECONDS)


class ComputeReclaimPolicy(ContractModel):
    """Reclaim windows applied by ``ComputeService`` reconciliation.

    ``provider_*`` maps override the default per configured provider name.
    """

    stale_grace_seconds: int = Field(default=DEFAULT_STALE_GRACE_SECONDS, gt=0)
    launch_intent_settle_seconds: int = Field(
        default=DEFAULT_LAUNCH_INTENT_SETTLE_SECONDS,
        gt=0,
    )
    bootstrap_phase_deadline_seconds: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_BOOTSTRAP_PHASE_DEADLINE_SECONDS)
    )
    max_launch_attempts: int = Field(default=DEFAULT_MAX_LAUNCH_ATTEMPTS, gt=0)
    provider_stale_grace_seconds: dict[str, int] = Field(default_factory=dict)
    provider_launch_intent_settle_seconds: dict[str, int] = Field(default_factory=dict)
    provider_bootstrap_phase_deadline_seconds: dict[str, dict[str, int]] = Field(
        default_factory=dict
    )
    provider_max_launch_attempts: dict[str, int] = Field(default_factory=dict)

    def stale_grace_for(self, provider: str) -> timedelta:
        return timedelta(
            seconds=self.provider_stale_grace_seconds.get(provider, self.stale_grace_seconds)
        )

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

    def launch_intent_settle_for(self, provider: str) -> timedelta:
        return timedelta(
            seconds=self.provider_launch_intent_settle_seconds.get(
                provider,
                self.launch_intent_settle_seconds,
            )
        )


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

    stale_grace_seconds: int = Field(default=DEFAULT_STALE_GRACE_SECONDS, gt=0)
    launch_intent_settle_seconds: int = Field(
        default=DEFAULT_LAUNCH_INTENT_SETTLE_SECONDS,
        gt=0,
    )
    bootstrap_phase_deadline_seconds: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_BOOTSTRAP_PHASE_DEADLINE_SECONDS)
    )
    max_launch_attempts: int = Field(default=DEFAULT_MAX_LAUNCH_ATTEMPTS, gt=0)
    provider_stale_grace_seconds: dict[str, int] = Field(default_factory=dict)
    provider_launch_intent_settle_seconds: dict[str, int] = Field(default_factory=dict)
    provider_bootstrap_phase_deadline_seconds: dict[str, dict[str, int]] = Field(
        default_factory=dict
    )
    provider_max_launch_attempts: dict[str, int] = Field(default_factory=dict)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_COMPUTE_RECLAIM_",
        env_file=".env",
        extra="ignore",
    )

    @field_validator(
        "provider_stale_grace_seconds",
        "provider_launch_intent_settle_seconds",
        "provider_max_launch_attempts",
    )
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
        return _validated_phase_deadlines(value)

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
            stale_grace_seconds=self.stale_grace_seconds,
            launch_intent_settle_seconds=self.launch_intent_settle_seconds,
            bootstrap_phase_deadline_seconds=self.bootstrap_phase_deadline_seconds,
            max_launch_attempts=self.max_launch_attempts,
            provider_stale_grace_seconds=self.provider_stale_grace_seconds,
            provider_launch_intent_settle_seconds=(self.provider_launch_intent_settle_seconds),
            provider_bootstrap_phase_deadline_seconds=(
                self.provider_bootstrap_phase_deadline_seconds
            ),
            provider_max_launch_attempts=self.provider_max_launch_attempts,
        )


__all__ = [
    "DEFAULT_BOOTSTRAP_PHASE_DEADLINE_SECONDS",
    "DEFAULT_LAUNCH_INTENT_SETTLE_SECONDS",
    "DEFAULT_MAX_LAUNCH_ATTEMPTS",
    "DEFAULT_STALE_GRACE_SECONDS",
    "ComputeReclaimPolicy",
    "ComputeReclaimSettings",
]
