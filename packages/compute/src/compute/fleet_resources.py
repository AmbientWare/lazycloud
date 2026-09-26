"Resource and offer values shared by fleet planning decisions."

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Capacity:
    cpu_millicores: int = 0
    memory_mib: int = 0
    gpu_count: int = 0

    def __add__(self, other: Capacity) -> Capacity:
        return Capacity(
            self.cpu_millicores + other.cpu_millicores,
            self.memory_mib + other.memory_mib,
            self.gpu_count + other.gpu_count,
        )

    def __sub__(self, other: Capacity) -> Capacity:
        return Capacity(
            self.cpu_millicores - other.cpu_millicores,
            self.memory_mib - other.memory_mib,
            self.gpu_count - other.gpu_count,
        )

    def __mul__(self, count: int) -> Capacity:
        return Capacity(
            self.cpu_millicores * count, self.memory_mib * count, self.gpu_count * count
        )

    def covers(self, other: Capacity) -> bool:
        """Whether this is at least `other` in every dimension."""
        return (
            self.cpu_millicores >= other.cpu_millicores
            and self.memory_mib >= other.memory_mib
            and self.gpu_count >= other.gpu_count
        )

    def clamped(self) -> Capacity:
        return self.upper(Capacity())

    def upper(self, other: Capacity) -> Capacity:
        return Capacity(
            max(self.cpu_millicores, other.cpu_millicores),
            max(self.memory_mib, other.memory_mib),
            max(self.gpu_count, other.gpu_count),
        )

    def lower(self, other: Capacity) -> Capacity:
        return Capacity(
            min(self.cpu_millicores, other.cpu_millicores),
            min(self.memory_mib, other.memory_mib),
            min(self.gpu_count, other.gpu_count),
        )

    def percent(self, percent: int) -> Capacity:
        return Capacity(
            -(-self.cpu_millicores * percent // 100),
            -(-self.memory_mib * percent // 100),
            -(-self.gpu_count * percent // 100),
        )

    @property
    def empty(self) -> bool:
        return Capacity().covers(self)


@dataclass(frozen=True, slots=True, order=True)
class ReserveMarket:
    """A purchase market the fleet keeps headroom in.

    GPU headroom is kept On-Demand. A stopped reserve costs only its disk in
    either market, and an On-Demand one serves Spot-tolerant work as well.
    """

    preemptible: bool
    gpu_type: str = ""

    @property
    def key(self) -> str:
        return f"{'spot' if self.preemptible else 'on-demand'}:{self.gpu_type or 'cpu'}"

    @classmethod
    def parse(cls, key: str) -> ReserveMarket:
        market, _, gpu = key.partition(":")
        return cls(preemptible=market == "spot", gpu_type="" if gpu == "cpu" else gpu)


@dataclass(frozen=True, slots=True)
class ReserveOffer:
    key: str
    market: ReserveMarket
    machine: Capacity
    nominal_cpu_millicores: int
    hourly_cost_micros: int
    stopped_hourly_cost_micros: int
    supports_reserve: bool
    preference_rank: tuple[int, ...] = ()
