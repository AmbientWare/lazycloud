"""Hot state the fleet reserve planner shares across scheduler replicas.

Redis holds only what a restart may lose: whose turn it is to plan, how long a
market has been short, which machines have been lightly used since when, the
last plan's targets, and the one consolidation each market may run. Every
decision these inform is re-read from PostgreSQL before it changes anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from coordination.redis_client import RedisClient
from coordination.redis_serialization import dump_model_json, load_model_json, redis_text
from pydantic import Field
from shared.contracts import ContractModel

from compute.fleet_policy import (
    RESERVE_EARLY_PLAN_INTERVAL_SECONDS,
    RESERVE_PLAN_INTERVAL_SECONDS,
    FleetReservePlan,
)
from compute.fleet_resources import Capacity, ReserveMarket

_PUBLISHED_SECONDS = 5 * 60


class MarketTargets(ContractModel):
    warm_target: Capacity
    stopped_target: Capacity


class PublishedReservePlan(ContractModel):
    """What the last plan left for the passes that run between plans."""

    targets: dict[str, MarketTargets] = Field(default_factory=dict)
    lightly_used_since: dict[str, datetime] = Field(default_factory=dict)

    @classmethod
    def of(cls, plan: FleetReservePlan) -> PublishedReservePlan:
        return cls(
            targets={
                market.market.key: MarketTargets(
                    warm_target=market.warm_target, stopped_target=market.stopped_target
                )
                for market in plan.markets
            },
            lightly_used_since=dict(plan.lightly_used_since),
        )


class Consolidation(ContractModel):
    machine_id: str
    unit_id: str
    workspace_id: str
    started_at: datetime
    stopped_at: datetime | None = None
    """When its movable containers were last told to stop; builds are waited out."""


class FleetReserveState(Protocol):
    def claim_plan(self, *, early: bool) -> bool: ...

    def pressure_ready(
        self, market: ReserveMarket, *, under_pressure: bool, now: datetime, sustained_seconds: int
    ) -> bool: ...

    def published(self) -> PublishedReservePlan: ...

    def publish(self, plan: FleetReservePlan) -> None: ...

    def consolidation_candidates(self) -> frozenset[str]: ...

    def consolidations(self) -> dict[ReserveMarket, Consolidation]: ...

    def cooling_markets(self) -> frozenset[ReserveMarket]: ...

    def begin_consolidation(
        self, market: ReserveMarket, consolidation: Consolidation, *, ttl_seconds: int
    ) -> bool: ...

    def save_consolidation(
        self, market: ReserveMarket, consolidation: Consolidation, *, ttl_seconds: int
    ) -> None: ...

    def finish_consolidation(self, market: ReserveMarket, *, cooldown_seconds: int) -> None: ...


@dataclass(frozen=True, slots=True)
class RedisFleetReserveState:
    redis: RedisClient

    def _key(self, *parts: str) -> str:
        return self.redis.key("compute", "fleet-reserve", *parts)

    def claim_plan(self, *, early: bool) -> bool:
        """Whether this replica plans now: once a minute fleet-wide, sooner under pressure."""
        if early and not self.redis.set(
            self._key("early"), "1", ex=RESERVE_EARLY_PLAN_INTERVAL_SECONDS, nx=True
        ):
            return False
        if early:
            self.redis.set(self._key("plan"), "1", ex=RESERVE_PLAN_INTERVAL_SECONDS)
            return True
        return self.redis.set(self._key("plan"), "1", ex=RESERVE_PLAN_INTERVAL_SECONDS, nx=True)

    def pressure_ready(
        self, market: ReserveMarket, *, under_pressure: bool, now: datetime, sustained_seconds: int
    ) -> bool:
        key = self._key("pressure", market.key)
        if not under_pressure:
            self.redis.delete(key)
            return False
        self.redis.set(key, str(now.timestamp()), ex=sustained_seconds * 2, nx=True)
        value = self.redis.get(key)
        return value is not None and now.timestamp() - float(redis_text(value)) >= sustained_seconds

    def published(self) -> PublishedReservePlan:
        raw = self.redis.get(self._key("published"))
        return (
            load_model_json(PublishedReservePlan, raw)
            if raw is not None
            else PublishedReservePlan()
        )

    def publish(self, plan: FleetReservePlan) -> None:
        self.redis.set(
            self._key("published"),
            dump_model_json(PublishedReservePlan.of(plan)),
            ex=_PUBLISHED_SECONDS,
        )
        # Kept apart from the plan: dispatch reads it on every batch.
        self.redis.set(
            self._key("candidates"),
            ",".join(
                sorted(
                    market.consolidation_candidate
                    for market in plan.markets
                    if market.consolidation_candidate
                )
            ),
            ex=_PUBLISHED_SECONDS,
        )

    def consolidation_candidates(self) -> frozenset[str]:
        raw = self.redis.get(self._key("candidates"))
        return frozenset(redis_text(raw).split(",")) - {""} if raw is not None else frozenset()

    def consolidations(self) -> dict[ReserveMarket, Consolidation]:
        markets = self.redis.set_members(self._key("consolidating"))
        found: dict[ReserveMarket, Consolidation] = {}
        for member in markets:
            market = ReserveMarket.parse(redis_text(member))
            raw = self.redis.get(self._key("consolidation", market.key))
            if raw is not None:
                found[market] = load_model_json(Consolidation, raw)
        return found

    def cooling_markets(self) -> frozenset[ReserveMarket]:
        return frozenset(
            market
            for member in self.redis.set_members(self._key("consolidating"))
            if (market := ReserveMarket.parse(redis_text(member)))
            and (
                self.redis.exists(self._key("consolidation", market.key))
                or self.redis.exists(self._key("cooldown", market.key))
            )
        )

    def begin_consolidation(
        self, market: ReserveMarket, consolidation: Consolidation, *, ttl_seconds: int
    ) -> bool:
        if self.redis.exists(self._key("cooldown", market.key)):
            return False
        if not self.redis.set(
            self._key("consolidation", market.key),
            dump_model_json(consolidation),
            ex=ttl_seconds,
            nx=True,
        ):
            return False
        self.redis.set_add(self._key("consolidating"), market.key)
        self.redis.expire(self._key("consolidating"), ttl_seconds)
        return True

    def save_consolidation(
        self, market: ReserveMarket, consolidation: Consolidation, *, ttl_seconds: int
    ) -> None:
        self.redis.set(
            self._key("consolidation", market.key), dump_model_json(consolidation), ex=ttl_seconds
        )

    def finish_consolidation(self, market: ReserveMarket, *, cooldown_seconds: int) -> None:
        if cooldown_seconds:
            self.redis.set(self._key("cooldown", market.key), "1", ex=cooldown_seconds)
        self.redis.delete(self._key("consolidation", market.key))


__all__ = [
    "Consolidation",
    "FleetReserveState",
    "MarketTargets",
    "PublishedReservePlan",
    "RedisFleetReserveState",
]
