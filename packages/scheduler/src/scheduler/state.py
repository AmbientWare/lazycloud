from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from math import ceil
from secrets import token_urlsafe

from coordination.redis_client import AsyncRedisClient, RedisClient, RedisWireScalar
from coordination.token_lock import (
    TokenLockReleaseStatus,
    release_token_lock,
    release_token_lock_async,
    try_acquire_token_lock,
    try_acquire_token_lock_async,
)
from pydantic import Field, field_validator
from shared.container_requests import StopContainerReason
from shared.contracts import ContractModel
from shared.gpu import gpu_preference_accepts
from shared.placement import ProductRegion
from shared.routing import AgentBackendRoute
from shared.scheduling import (
    DEFAULT_CONTAINER_STATE_TTL_SECONDS,
    ContainerIpAssignment,
    ContainerStatusUpdatePlan,
    NetworkIpMutationAction,
    NetworkIpMutationPlan,
    SchedulerContainerAddress,
    SchedulerContainerAddressMap,
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    SchedulerWorkerStatus,
    WorkerCapacityChange,
    WorkerCapacityPlan,
    WorkerCapacityResult,
    WorkerExecutionRequest,
    WorkerRemovalResult,
    WorkerRepositoryLockKind,
    WorkerRepositoryLockRecord,
    WorkerRepositoryLockRelease,
    WorkerUnavailableReason,
    gpu_count_for_capacity,
)
from shared.timestamps import utc_now

from coordination import redis_serialization
from scheduler.fleet import (
    WorkerPoolStateSnapshot,
)
from scheduler.preemption import (
    WorkerPlannedDrainOperation,
    WorkerPreemptionOperation,
    WorkerPreemptionQueueResult,
)

DEFAULT_WORKER_STATE_TTL_SECONDS = 60
DEFAULT_PENDING_WORKER_STATE_TTL_SECONDS = 900
DEFAULT_CONTAINER_EXIT_CODE_TTL_SECONDS = 86_400
DEFAULT_CONTAINER_CANCELLATION_TTL_SECONDS = 86_400
DEFAULT_WORKER_LOCK_TTL_SECONDS = 10
DEFAULT_WORKER_LOCK_RETRIES = 3
DEFAULT_CONTAINER_LOCK_TTL_SECONDS = 10
DEFAULT_CONTAINER_LOCK_RETRIES = 3
DEFAULT_IMAGE_PULL_LOCK_TTL_SECONDS = 30
DEFAULT_IMAGE_PULL_LOCK_RETRIES = 600
DEFAULT_NETWORK_LOCK_TTL_SECONDS = 30
DEFAULT_CONCURRENCY_LOCK_TTL_SECONDS = 15
DEFAULT_CONCURRENCY_COUNTER_WAIT_SECONDS = 15.0
DEFAULT_CONCURRENCY_COUNTER_POLL_SECONDS = 0.1
DEFAULT_CONCURRENCY_COUNTER_REPAIR_INTERVAL_SECONDS = 5.0
DEFAULT_CONCURRENCY_RESERVATION_IN_FLIGHT_TTL_SECONDS = 120.0
DEFAULT_CONTAINER_REQUEST_CLAIM_LEASE_SECONDS = 60.0

ENQUEUE_CONTAINER_REQUEST_SCRIPT = """
if redis.call("EXISTS", KEYS[3]) == 1 then
    return 0
end
local worker = redis.call("HGET", KEYS[4], "worker_id")
local status = redis.call("HGET", KEYS[4], "status")
if (worker and cjson.decode(worker) ~= "")
    or (status and cjson.decode(status) ~= "pending")
    or redis.call("HEXISTS", KEYS[2], ARGV[1]) == 1 then
    return 0
end
redis.call("HSET", KEYS[2], ARGV[1], ARGV[2])
return redis.call("ZADD", KEYS[1], ARGV[3], ARGV[1])
"""

CLAIM_READY_CONTAINER_REQUESTS_SCRIPT = """
local expired_ids = redis.call("ZRANGEBYSCORE", KEYS[3], "-inf", ARGV[1])
for _, request_id in ipairs(expired_ids) do
    redis.call("ZREM", KEYS[3], request_id)
    redis.call("HDEL", KEYS[4], request_id)
    if redis.call("HEXISTS", KEYS[2], request_id) == 1 then
        redis.call("ZADD", KEYS[1], ARGV[1], request_id)
    end
end
local request_ids = redis.call("ZRANGEBYSCORE", KEYS[1], "-inf", ARGV[1], "LIMIT", 0, ARGV[2])
local requests = {}
for _, request_id in ipairs(request_ids) do
    local payload = redis.call("HGET", KEYS[2], request_id)
    redis.call("ZREM", KEYS[1], request_id)
    if payload then
        redis.call("ZADD", KEYS[3], ARGV[4], request_id)
        redis.call("HSET", KEYS[4], request_id, ARGV[3])
        table.insert(requests, request_id)
        table.insert(requests, payload)
    end
end
return requests
"""

ACKNOWLEDGE_CONTAINER_REQUEST_SCRIPT = """
if redis.call("HGET", KEYS[2], ARGV[1]) ~= ARGV[2] then
    return 0
end
redis.call("ZREM", KEYS[1], ARGV[1])
redis.call("HDEL", KEYS[2], ARGV[1])
redis.call("HDEL", KEYS[3], ARGV[1])
return 1
"""

REQUEUE_DRAINED_WORKER_REQUEST_SCRIPT = """
if redis.call("EXISTS", KEYS[3]) == 1 then
    return 0
end
local status = redis.call("HGET", KEYS[4], "status")
if status and cjson.decode(status) ~= "pending" then
    return 0
end
local worker = redis.call("HGET", KEYS[4], "worker_id")
if worker and cjson.decode(worker) ~= "" and cjson.decode(worker) ~= ARGV[4] then
    return 0
end
if status then
    redis.call("HSET", KEYS[4], "worker_id", cjson.encode(""))
end
redis.call("SREM", KEYS[5], KEYS[4])
if redis.call("HEXISTS", KEYS[2], ARGV[1]) == 0 then
    redis.call("HSET", KEYS[2], ARGV[1], ARGV[2])
    redis.call("ZADD", KEYS[1], ARGV[3], ARGV[1])
end
return 1
"""

REQUEUE_CONTAINER_REQUEST_SCRIPT = """
if redis.call("HGET", KEYS[4], ARGV[1]) ~= ARGV[2] then
    return -1
end
redis.call("ZREM", KEYS[3], ARGV[1])
redis.call("HDEL", KEYS[4], ARGV[1])
if redis.call("EXISTS", KEYS[5]) == 1 then
    redis.call("HDEL", KEYS[2], ARGV[1])
    return 0
end
redis.call("HSET", KEYS[2], ARGV[1], ARGV[3])
redis.call("ZADD", KEYS[1], ARGV[4], ARGV[1])
return 1
"""

FENCE_CONTAINER_REQUEST_SCRIPT = """
redis.call("SET", KEYS[5], "1", "EX", ARGV[2])
local removed = redis.call("ZREM", KEYS[1], ARGV[1])
removed = removed + redis.call("HDEL", KEYS[2], ARGV[1])
removed = removed + redis.call("ZREM", KEYS[3], ARGV[1])
removed = removed + redis.call("HDEL", KEYS[4], ARGV[1])
return removed
"""

PLACE_WORKER_REQUEST_SCRIPT = """
if redis.call("EXISTS", KEYS[3]) == 1 then
    return 0
end
redis.call("LREM", KEYS[1], 0, ARGV[1])
redis.call("LREM", KEYS[4], 0, ARGV[1])
redis.call("HSET", KEYS[2], ARGV[1], ARGV[2])
redis.call("RPUSH", KEYS[1], ARGV[1])
return 1
"""

DISPATCH_CLAIMED_WORKER_REQUEST_SCRIPT = """
if redis.call("EXISTS", KEYS[4]) == 1 then
    return -2
end
if redis.call("HGET", KEYS[8], ARGV[1]) ~= ARGV[2] then
    return -1
end
if redis.call("HGET", KEYS[1], "resource_version") ~= ARGV[4] then
    return -3
end
if ARGV[5] == "1" and redis.call("GET", KEYS[9]) ~= ARGV[6] then
    return -4
end
local request = cjson.decode(ARGV[3])
if request.backfill == true then
    local function worker_field(name, absent)
        local raw = redis.call("HGET", KEYS[1], name)
        if raw then return cjson.decode(raw) end
        return absent
    end
    local total_gpu = worker_field("total_gpu_count", 0)
    if request.preemptible ~= true or request.gpu_count ~= 0 or #request.gpu ~= 0
        or total_gpu <= 0 or worker_field("free_gpu_count", 0) >= total_gpu then
        return -5
    end
    local recovery = redis.call("GET", KEYS[13])
    if recovery and redis.call("HEXISTS", KEYS[6], recovery) == 1 then return -5 end
    if recovery then redis.call("DEL", KEYS[13]) end
    local gpu_matches = cjson.decode(ARGV[7])
    for _, payload in ipairs(redis.call("HVALS", KEYS[6])) do
        local queued = cjson.decode(payload)
        local requested_gpu = math.max(queued.gpu_count, #queued.gpu > 0 and 1 or 0)
        if requested_gpu > 0 and requested_gpu <= total_gpu
            and (queued.pool_selector == "" or queued.pool_selector == worker_field("pool", ""))
            and (queued.region == cjson.null or queued.region == nil
                or queued.region == worker_field("region", cjson.null))
            and (queued.availability_zone == nil or queued.availability_zone == ""
                or queued.availability_zone == worker_field("availability_zone", "")) then
            for _, gpu in ipairs(queued.gpu) do
                if gpu_matches[gpu] ~= false then return -5 end
            end
        end
    end
    redis.call("HSET", KEYS[14], "backfill", "true", "preemptible", "true")
end
redis.call("HSET", KEYS[1], unpack(ARGV, 8, #ARGV))
redis.call("LREM", KEYS[2], 0, ARGV[1])
redis.call("LREM", KEYS[12], 0, ARGV[1])
redis.call("HSET", KEYS[3], ARGV[1], ARGV[3])
redis.call("RPUSH", KEYS[2], ARGV[1])
redis.call("ZREM", KEYS[5], ARGV[1])
redis.call("HDEL", KEYS[6], ARGV[1])
redis.call("ZREM", KEYS[7], ARGV[1])
redis.call("HDEL", KEYS[8], ARGV[1])
if redis.call("GET", KEYS[13]) == ARGV[1] then redis.call("DEL", KEYS[13]) end
if ARGV[5] == "1" then
    redis.call("DEL", KEYS[9])
    redis.call("DEL", KEYS[10])
    redis.call("SREM", KEYS[11], ARGV[1])
end
return 1
"""

PREEMPT_GPU_BACKFILL_SCRIPT = """
if redis.call("HGET", KEYS[1], "resource_version") ~= ARGV[1] then return {"stale"} end
local payload = redis.call("HGET", KEYS[3], ARGV[2])
if not payload then return {"gone"} end
local request = cjson.decode(payload)
if request.gpu_count <= 0 and #request.gpu == 0 then return {"invalid"} end
local existing = redis.call("GET", KEYS[2])
if existing and existing ~= ARGV[2] and redis.call("HEXISTS", KEYS[3], existing) == 1 then
    return {"busy"}
end
local selected = {}
for index = 4, #KEYS do
    if redis.call("HGET", KEYS[index], "backfill") == "true"
        and redis.call("HGET", KEYS[index], "preemptible") == "true"
        and redis.call("HGET", KEYS[index], "gpu_count") == "0"
        and redis.call("HGET", KEYS[index], "worker_id") == ARGV[3] then
        local status = redis.call("HGET", KEYS[index], "status")
        local lease = redis.call("HGET", KEYS[index], "backfill_eviction_claim_until")
        local until_time = tonumber(lease or "0")
        if (status == '"pending"' or status == '"running"') and until_time <= tonumber(ARGV[4]) then
            redis.call("HSET", KEYS[index], "backfill_eviction_requested", "true",
                "backfill_eviction_claim_until", tostring(tonumber(ARGV[4]) + 60))
            table.insert(selected, cjson.decode(redis.call("HGET", KEYS[index], "container_id")))
        end
    end
end
if #selected > 0 then redis.call("SET", KEYS[2], ARGV[2]) end
table.insert(selected, 1, "ok")
return selected
"""

TAKE_WORKER_REQUEST_SCRIPT = """
while true do
    local request_id = redis.call("LINDEX", KEYS[3], 0)
    if not request_id then
        break
    end
    local payload = redis.call("HGET", KEYS[2], request_id)
    if payload then
        return {request_id, payload}
    end
    redis.call("LPOP", KEYS[3])
end
while redis.call("LLEN", KEYS[1]) > 0 do
    local request_id = redis.call("LPOP", KEYS[1])
    local payload = redis.call("HGET", KEYS[2], request_id)
    if payload then
        redis.call("LREM", KEYS[3], 0, request_id)
        redis.call("RPUSH", KEYS[3], request_id)
        return {request_id, payload}
    end
end
return {}
"""

CLAIM_MOVED_WORKER_REQUEST_SCRIPT = """
local payload = redis.call("HGET", KEYS[1], ARGV[1])
if payload then
    return payload
end
redis.call("LREM", KEYS[2], 0, ARGV[1])
return ""
"""

ACKNOWLEDGE_WORKER_REQUEST_SCRIPT = """
if redis.call("LREM", KEYS[1], 0, ARGV[1]) == 0 then
    return 0
end
redis.call("HDEL", KEYS[2], ARGV[1])
return 1
"""

RETURN_WORKER_REQUEST_SCRIPT = """
redis.call("LREM", KEYS[1], 0, ARGV[1])
redis.call("LREM", KEYS[2], 0, ARGV[1])
redis.call("HDEL", KEYS[3], ARGV[1])
if redis.call("EXISTS", KEYS[6]) == 1 then
    return 0
end
redis.call("HSET", KEYS[5], ARGV[1], ARGV[2])
return redis.call("ZADD", KEYS[4], ARGV[3], ARGV[1])
"""

CANCEL_WORKER_REQUEST_SCRIPT = """
local payload = redis.call("HGET", KEYS[2], ARGV[1])
local removed = redis.call("LREM", KEYS[1], 0, ARGV[1])
removed = removed + redis.call("LREM", KEYS[3], 0, ARGV[1])
redis.call("HDEL", KEYS[2], ARGV[1])
return {removed, payload or ""}
"""

DRAIN_WORKER_REQUESTS_SCRIPT = """
local queued = {}
for _, request_id in ipairs(redis.call("LRANGE", KEYS[1], 0, -1)) do
    local payload = redis.call("HGET", KEYS[2], request_id)
    if payload then
        table.insert(queued, payload)
    end
end
local delivered = {}
for _, request_id in ipairs(redis.call("LRANGE", KEYS[3], 0, -1)) do
    local payload = redis.call("HGET", KEYS[2], request_id)
    if payload then
        table.insert(delivered, payload)
    end
end
redis.call("DEL", KEYS[1])
redis.call("DEL", KEYS[2])
redis.call("DEL", KEYS[3])
local drained = {tostring(#queued)}
for _, payload in ipairs(queued) do
    table.insert(drained, payload)
end
for _, payload in ipairs(delivered) do
    table.insert(drained, payload)
end
return drained
"""

PREEMPT_WORKER_REQUESTS_SCRIPT = """
if redis.call("GET", KEYS[6]) == ARGV[2] then
    return {0}
end
if redis.call("EXISTS", KEYS[1]) == 0 then
    return {-1}
end
if redis.call("HGET", KEYS[1], "resource_version") ~= ARGV[1]
    or redis.call("HGET", KEYS[1], "capacity_owner_id") ~= ARGV[7]
    or (ARGV[8] ~= "" and redis.call("HGET", KEYS[1], "machine_id") ~= ARGV[8]) then
    return {-2}
end
redis.call("HSET", KEYS[1],
    "status", ARGV[4],
    "resource_version", ARGV[5],
    "updated_at", ARGV[6])
local requeued = {1}
local request_count = tonumber(ARGV[3])
for index = 1, request_count do
    local request_id = ARGV[11 + ((index - 1) * 2) + 1]
    local payload = ARGV[11 + ((index - 1) * 2) + 2]
    redis.call("LREM", KEYS[2], 0, request_id)
    redis.call("LREM", KEYS[7], 0, request_id)
    redis.call("HDEL", KEYS[3], request_id)
    local cancelled = redis.call("EXISTS", KEYS[7 + index]) == 1
    -- Any status but pending: a worker acted on this request, and whether the
    -- container is still running or has already finished, requeueing it now
    -- would run the work a second time. A missing hash is a request nothing
    -- acted on, which `false` reports as not started.
    local status = redis.call("HGET", KEYS[7 + request_count + index], "status")
    local started = status and status ~= ARGV[11]
    if not cancelled and not started then
        redis.call("HSET", KEYS[5], request_id, payload)
        redis.call("ZADD", KEYS[4], ARGV[9], request_id)
        table.insert(requeued, request_id)
    end
end
redis.call("SET", KEYS[6], ARGV[2], "EX", ARGV[10])
return requeued
"""

CLAIM_WORKER_ROLLOUT_SLOT_SCRIPT = """
local expired = redis.call("ZRANGEBYSCORE", KEYS[1], "-inf", ARGV[3])
for _, worker_id in ipairs(expired) do
    redis.call("ZREM", KEYS[1], worker_id)
    redis.call("HDEL", KEYS[2], worker_id)
end
if redis.call("ZSCORE", KEYS[1], ARGV[1]) then
    redis.call("ZADD", KEYS[1], ARGV[4], ARGV[1])
    redis.call("HSET", KEYS[2], ARGV[1], ARGV[2])
    redis.call("EXPIRE", KEYS[1], ARGV[6])
    redis.call("EXPIRE", KEYS[2], ARGV[6])
    return 1
end
if redis.call("ZCARD", KEYS[1]) >= tonumber(ARGV[5]) then
    return 0
end
redis.call("ZADD", KEYS[1], ARGV[4], ARGV[1])
redis.call("HSET", KEYS[2], ARGV[1], ARGV[2])
redis.call("EXPIRE", KEYS[1], ARGV[6])
redis.call("EXPIRE", KEYS[2], ARGV[6])
return 1
"""

RELEASE_WORKER_ROLLOUT_SLOT_SCRIPT = """
if redis.call("HGET", KEYS[2], ARGV[1]) ~= ARGV[2] then
    return 0
end
redis.call("ZREM", KEYS[1], ARGV[1])
redis.call("HDEL", KEYS[2], ARGV[1])
if redis.call("ZCARD", KEYS[1]) == 0 then
    redis.call("DEL", KEYS[1], KEYS[2])
end
return 1
"""

RESERVE_CONCURRENCY_SCRIPT = """
if redis.call("EXISTS", KEYS[2]) == 1 then
    redis.call("SADD", KEYS[3], ARGV[7])
    return {ARGV[11], "0"}
end

if redis.call("HGET", KEYS[1], "initialized") ~= ARGV[9]
    or redis.call("HGET", KEYS[1], "repairing") == ARGV[9] then
    return {ARGV[12], "0"}
end

local used_gpu = tonumber(redis.call("HGET", KEYS[1], "gpu_count") or "0")
local used_cpu = tonumber(redis.call("HGET", KEYS[1], "cpu_millicores") or "0")
local workspace_gpu_quota = tonumber(ARGV[1])
local cpu_limit = tonumber(ARGV[2])
local request_gpu = tonumber(ARGV[3])
local request_cpu = tonumber(ARGV[4])

if used_gpu + request_gpu > workspace_gpu_quota then
    return {ARGV[13], "0"}
end

if used_cpu + request_cpu > cpu_limit then
    return {ARGV[14], "0"}
end

redis.call("HINCRBY", KEYS[1], "gpu_count", request_gpu)
redis.call("HINCRBY", KEYS[1], "cpu_millicores", request_cpu)
redis.call("HSET", KEYS[1],
    "initialized", ARGV[9],
    "repairing", ARGV[10],
    "updated_at", ARGV[8])
redis.call("HSET", KEYS[2],
    "workspace_id", ARGV[5],
    "container_id", ARGV[6],
    "gpu_count", ARGV[3],
    "cpu_millicores", ARGV[4],
    "created_at", ARGV[8])
redis.call("SADD", KEYS[3], ARGV[7])
return {ARGV[11], "1"}
"""

RELEASE_CONCURRENCY_SCRIPT = """
if redis.call("EXISTS", KEYS[2]) == 0 then
    redis.call("SREM", KEYS[3], ARGV[2])
    return {ARGV[6], "0"}
end

if redis.call("HGET", KEYS[1], "initialized") ~= ARGV[3]
    or redis.call("HGET", KEYS[1], "repairing") == ARGV[3] then
    return {ARGV[7], "0"}
end

local reserved_gpu = tonumber(redis.call("HGET", KEYS[2], "gpu_count") or "0")
local reserved_cpu = tonumber(redis.call("HGET", KEYS[2], "cpu_millicores") or "0")
local used_gpu = tonumber(redis.call("HGET", KEYS[1], "gpu_count") or "0")
local used_cpu = tonumber(redis.call("HGET", KEYS[1], "cpu_millicores") or "0")

if reserved_gpu ~= 0 then
    used_gpu = redis.call("HINCRBY", KEYS[1], "gpu_count", -reserved_gpu)
end
if reserved_cpu ~= 0 then
    used_cpu = redis.call("HINCRBY", KEYS[1], "cpu_millicores", -reserved_cpu)
end
if used_gpu < 0 then
    redis.call("HSET", KEYS[1], "gpu_count", 0)
end
if used_cpu < 0 then
    redis.call("HSET", KEYS[1], "cpu_millicores", 0)
end

redis.call("HSET", KEYS[1],
    "initialized", ARGV[3],
    "repairing", ARGV[4],
    "updated_at", ARGV[1])
redis.call("DEL", KEYS[2])
redis.call("SREM", KEYS[3], ARGV[2])
return {ARGV[5], "1"}
"""

CONTAINER_STATUSES_SCRIPT = """
local statuses = {}
for index = 1, #KEYS do
    statuses[index] = redis.call("HGET", KEYS[index], "status") or ""
end
return statuses
"""

UPDATABLE_CONTAINER_STATUSES = frozenset(
    {
        SchedulerContainerStatus.Pending,
        SchedulerContainerStatus.Running,
        SchedulerContainerStatus.Stopping,
        SchedulerContainerStatus.Complete,
        SchedulerContainerStatus.Failed,
    }
)


class ConcurrencyReservationStatus(StrEnum):
    Ok = "ok"
    Repairing = "repairing"
    GpuExceeded = "gpu-exceeded"
    CpuExceeded = "cpu-exceeded"
    Missing = "missing"


@dataclass(frozen=True, slots=True)
class SchedulerContainerRequestClaim:
    request: SchedulerWorkerRequest
    token: str


class ConcurrencyCounter(ContractModel):
    workspace_id: str
    initialized: bool = True
    repairing: bool = False
    gpu_count: int = 0
    cpu_millicores: int = 0
    updated_at: datetime = Field(default_factory=utc_now)
    repair_started_at: datetime | None = None
    repaired_at: datetime | None = None

    @field_validator("gpu_count", "cpu_millicores")
    @classmethod
    def counter_values_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "concurrency counter values cannot be negative"
            raise ValueError(msg)
        return value


class ConcurrencyReservation(ContractModel):
    workspace_id: str
    container_id: str
    gpu_count: int = 0
    cpu_millicores: int = 0
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("gpu_count", "cpu_millicores")
    @classmethod
    def reservation_values_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "concurrency reservation values cannot be negative"
            raise ValueError(msg)
        return value


class ConcurrencyReservationDecision(ContractModel):
    status: ConcurrencyReservationStatus
    counter: ConcurrencyCounter
    reservation: ConcurrencyReservation | None = None
    reason: str = ""
    changed: bool = False


@dataclass(slots=True)
class WorkerReservedCapacity:
    cpu_millicores: int = 0
    memory_mib: int = 0
    gpu_count: int = 0


@dataclass(frozen=True, slots=True)
class WorkerRequestDrain:
    """Everything one worker held, split by whether it had been handed out.

    Delivery does not prove execution. Recovery checks the container's current
    assignment and status before returning either kind to the queue.
    """

    queued: list[str] = field(default_factory=list)
    delivered: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CapacityReservationDispatchAllocation:
    reservation_id: str
    request_index_key: str
    allocation_key: str
    allocation_index_key: str


class SchedulerRepositoryError(RuntimeError):
    pass


class ContainerRequestClaimNotOwnedError(SchedulerRepositoryError):
    pass


class ContainerRequestCancelledError(SchedulerRepositoryError):
    pass


class WorkerStateNotFoundError(SchedulerRepositoryError):
    pass


class ContainerStateNotFoundError(SchedulerRepositoryError):
    pass


class WorkerPoolStateNotFoundError(SchedulerRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class SchedulerStateKeys:
    redis: RedisClient | AsyncRedisClient
    namespace: str = "scheduler"

    def orphaned_container_confirmation(self, container_id: str) -> str:
        return self.redis.key(self.namespace, "orphaned-containers", container_id)

    def worker_state(self, worker_id: str) -> str:
        return self.redis.key(self.namespace, "workers", worker_id, "state")

    def worker_id_from_state_key(self, state_key: str) -> str:
        prefix = f"{self.redis.key(self.namespace, 'workers')}:"
        suffix = ":state"
        if not state_key.startswith(prefix) or not state_key.endswith(suffix):
            return ""
        return state_key.removeprefix(prefix).removesuffix(suffix).strip(":")

    def worker_index(self) -> str:
        return self.redis.key(self.namespace, "workers", "index")

    def worker_lock(self, worker_id: str) -> str:
        return self.redis.key(self.namespace, "workers", worker_id, "lock")

    def worker_backfill_recovery(self, worker_id: str) -> str:
        return self.redis.key(self.namespace, "workers", worker_id, "backfill-recovery")

    def worker_requests(self, worker_id: str) -> str:
        return self.redis.key(self.namespace, "workers", worker_id, "requests")

    def worker_request_payloads(self, worker_id: str) -> str:
        return self.redis.key(self.namespace, "workers", worker_id, "request-payloads")

    def worker_inflight_requests(self, worker_id: str) -> str:
        """Requests handed to this worker and not yet acknowledged by it.

        A delivery moves the id here in the same command that removes it from the
        queue, and only the worker's acknowledgement removes it. The payload stays
        in `worker_request_payloads` throughout, so a request in flight is still a
        request the scheduler can see, requeue, and cancel.
        """

        return self.redis.key(self.namespace, "workers", worker_id, "inflight-requests")

    def worker_preemption_operation(self, worker_id: str, operation_id: str) -> str:
        operation_digest = sha256(operation_id.encode()).hexdigest()
        return self.redis.key(
            self.namespace,
            "workers",
            worker_id,
            "preemption-operations",
            operation_digest,
        )

    def image_pull_lock(self, worker_id: str, image_id: str) -> str:
        return self.redis.key("worker-images", worker_id, "images", image_id, "pull-lock")

    def container_requests(self) -> str:
        return self.redis.key(self.namespace, "containers", "requests")

    def container_request_payloads(self) -> str:
        return self.redis.key(self.namespace, "containers", "request-payloads")

    def container_request_claims(self) -> str:
        return self.redis.key(self.namespace, "containers", "request-claims")

    def container_request_claim_owners(self) -> str:
        return self.redis.key(self.namespace, "containers", "request-claim-owners")

    def container_state(self, container_id: str) -> str:
        return self.redis.key(self.namespace, "containers", container_id, "state")

    def container_cancellation(self, container_id: str) -> str:
        return self.redis.key(self.namespace, "containers", container_id, "cancelled")

    def container_lock(self, container_id: str) -> str:
        return self.redis.key(self.namespace, "containers", container_id, "lock")

    def container_stub_index(self, stub_id: str) -> str:
        return self.redis.key(self.namespace, "containers", "stub", stub_id, "index")

    def container_workspace_index(self, workspace_id: str) -> str:
        return self.redis.key(self.namespace, "containers", "workspace", workspace_id, "index")

    def container_workspace_ownership_index(self, workspace_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "containers",
            "workspace",
            workspace_id,
            "ownership-index",
        )

    def container_worker_index(self, worker_id: str) -> str:
        return self.redis.key(self.namespace, "containers", "worker", worker_id, "index")

    def container_exit_code(self, container_id: str) -> str:
        return self.redis.key(self.namespace, "containers", container_id, "exit-code")

    def container_termination_reason(self, container_id: str) -> str:
        return self.redis.key(self.namespace, "containers", container_id, "termination-reason")

    def container_address(self, container_id: str) -> str:
        return self.redis.key(self.namespace, "containers", container_id, "address")

    def container_address_map(self, container_id: str) -> str:
        return self.redis.key(self.namespace, "containers", container_id, "address-map")

    def worker_address(self, container_id: str) -> str:
        return self.redis.key(self.namespace, "containers", container_id, "worker-address")

    def workspace_concurrency_counter(self, workspace_id: str) -> str:
        return self.redis.key(self.namespace, "workspace-concurrency", workspace_id, "counter")

    def workspace_concurrency_reservation(self, workspace_id: str, container_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "workspace-concurrency",
            workspace_id,
            "reservations",
            container_id,
        )

    def workspace_concurrency_reservation_index(self, workspace_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "workspace-concurrency",
            workspace_id,
            "reservations",
            "index",
        )

    def workspace_concurrency_lock(self, workspace_id: str) -> str:
        return self.redis.key(self.namespace, "workspace-concurrency", workspace_id, "lock")

    def worker_pool_state(self, pool: str) -> str:
        return self.redis.key(self.namespace, "worker-pools", pool, "state")

    def worker_pool_replicas(self, pool: str) -> str:
        return self.redis.key(self.namespace, "worker-pools", pool, "replicas")

    def worker_pool_lock(self, pool: str) -> str:
        return self.redis.key(self.namespace, "worker-pools", pool, "state-lock")

    def capacity_owner_mutation_lock(self, capacity_owner_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "capacity-owners",
            capacity_owner_key_segment(capacity_owner_id),
            "mutation-lock",
        )

    def worker_rollout_slots(self, capacity_owner_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "capacity-owners",
            capacity_owner_key_segment(capacity_owner_id),
            "worker-rollout-slots",
        )

    def worker_rollout_revisions(self, capacity_owner_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "capacity-owners",
            capacity_owner_key_segment(capacity_owner_id),
            "worker-rollout-revisions",
        )

    def network_container_ip(self, network_prefix: str, container_id: str) -> str:
        return self.redis.key("worker-network", network_prefix, "containers", container_id, "ip")

    def network_container_prefixes(self, container_id: str) -> str:
        return self.redis.key("worker-network", "containers", container_id, "prefixes")

    def network_ip_index(self, network_prefix: str) -> str:
        return self.redis.key("worker-network", network_prefix, "ips")

    def network_ip_ref_counts(self, network_prefix: str) -> str:
        return self.redis.key("worker-network", network_prefix, "ip-ref-counts")

    def network_ip_owner(self, network_prefix: str, ip_address: str) -> str:
        return self.redis.key("worker-network", network_prefix, "ip-owner", ip_address)

    def network_lock(self, network_prefix: str) -> str:
        return self.redis.key("worker-network", network_prefix, "lock")


@dataclass(init=False, slots=True)
class RedisSchedulerWorkerRepository:
    redis: RedisClient
    keys: SchedulerStateKeys

    def __init__(self, redis: RedisClient, keys: SchedulerStateKeys | None = None) -> None:
        self.redis = redis
        self.keys = keys or SchedulerStateKeys(redis)

    def add_worker(
        self,
        worker: SchedulerWorkerRecord,
        *,
        ttl_seconds: int = DEFAULT_PENDING_WORKER_STATE_TTL_SECONDS,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord:
        def write() -> SchedulerWorkerRecord:
            state_key = self.keys.worker_state(worker.worker_id)
            current_time = now or utc_now()
            record = worker.model_copy(
                update={
                    "resource_version": 0,
                    "updated_at": current_time,
                    "created_at": worker.created_at,
                }
            )
            self.redis.set_add(self.keys.worker_index(), state_key)
            self.redis.hash_set(state_key, mapping=redis_serialization.dump_model_hash(record))
            self.redis.expire(state_key, ttl_seconds)
            return record

        return self._with_worker_lock(worker.worker_id, write)

    def get_worker(self, worker_id: str) -> SchedulerWorkerRecord | None:
        return self._get_worker_from_key(self.keys.worker_state(worker_id))

    async def get_worker_async(
        self,
        redis: AsyncRedisClient,
        worker_id: str,
    ) -> SchedulerWorkerRecord | None:
        raw = await redis.hash_get_all(self.keys.worker_state(worker_id))
        if not raw:
            return None
        return redis_serialization.load_model_hash(SchedulerWorkerRecord, raw)

    def list_workers(self) -> list[SchedulerWorkerRecord]:
        state_keys = sorted(
            redis_serialization.redis_strings(self.redis.set_members(self.keys.worker_index()))
        )
        workers: list[SchedulerWorkerRecord] = []
        for state_key in state_keys:
            worker = self._get_worker_from_key(state_key)
            if worker is None:
                self._cleanup_missing_worker_state(state_key, now=utc_now())
                continue
            workers.append(worker)
        return workers

    def cleanup_missing_workers(
        self,
        *,
        now: datetime | None = None,
    ) -> list[WorkerRemovalResult]:
        requeue_time = now or utc_now()
        results: list[WorkerRemovalResult] = []
        state_keys = sorted(
            redis_serialization.redis_strings(self.redis.set_members(self.keys.worker_index()))
        )
        for state_key in state_keys:
            if self.redis.hash_get_all(state_key):
                continue
            result = self._cleanup_missing_worker_state(state_key, now=requeue_time)
            if result is not None:
                results.append(result)
        return results

    def _cleanup_missing_worker_state(
        self,
        state_key: str,
        *,
        now: datetime,
    ) -> WorkerRemovalResult | None:
        """Reclaim a worker whose keepalive lapsed, including what it was handed.

        This is the bound on an in-flight request: it is reclaimed when the worker
        that holds it stops proving it is alive, not on a clock of its own. A
        worker that is up re-arms its state key and keeps its own deliveries, and
        gets them back on its next poll.
        """

        worker_id = self.keys.worker_id_from_state_key(state_key)
        self.redis.set_remove(self.keys.worker_index(), state_key)
        if not worker_id:
            return None
        drain = self.drain_worker_requests(worker_id)
        try:
            request_ids = self._requeue_drained_worker_requests(worker_id, drain, now=now)
        except Exception:
            self.restore_worker_requests(worker_id, drain)
            self.redis.set_add(self.keys.worker_index(), state_key)
            raise
        self.redis.delete(state_key)
        return WorkerRemovalResult(
            worker_id=worker_id,
            removed=True,
            requeued_count=len(request_ids),
            request_ids=request_ids,
        )

    def list_workers_in_pool(self, pool: str) -> list[SchedulerWorkerRecord]:
        return [worker for worker in self.list_workers() if worker.pool == pool]

    def list_workers_for_capacity_owner(
        self,
        capacity_owner_id: str,
    ) -> list[SchedulerWorkerRecord]:
        return [
            worker
            for worker in self.list_workers()
            if worker.capacity_owner_id == capacity_owner_id
        ]

    def list_workers_on_machine(self, machine_id: str) -> list[SchedulerWorkerRecord]:
        return [worker for worker in self.list_workers() if worker.machine_id == machine_id]

    def update_worker_status(
        self,
        worker_id: str,
        status: SchedulerWorkerStatus,
        *,
        ttl_seconds: int = DEFAULT_WORKER_STATE_TTL_SECONDS,
        now: datetime | None = None,
        reconcile_capacity: bool = False,
        unavailable_reason: WorkerUnavailableReason | None = None,
        unavailable_detail: str = "",
    ) -> SchedulerWorkerRecord:
        def write() -> SchedulerWorkerRecord:
            worker = self.get_worker(worker_id)
            if worker is None:
                raise WorkerStateNotFoundError(worker_id)
            if reconcile_capacity:
                worker = self._reconciled_worker_capacity(worker)
            updated = _worker_with_status(
                worker,
                status,
                now=now,
                unavailable_reason=unavailable_reason,
                unavailable_detail=unavailable_detail,
            )
            state_key = self.keys.worker_state(worker_id)
            self.redis.hash_set(state_key, mapping=redis_serialization.dump_model_hash(updated))
            self.redis.expire(state_key, ttl_seconds)
            return updated

        return self._with_worker_lock(worker_id, write)

    async def update_worker_status_async(
        self,
        redis: AsyncRedisClient,
        worker_id: str,
        status: SchedulerWorkerStatus,
        *,
        ttl_seconds: int = DEFAULT_WORKER_STATE_TTL_SECONDS,
        now: datetime | None = None,
        unavailable_reason: WorkerUnavailableReason | None = None,
        unavailable_detail: str = "",
    ) -> SchedulerWorkerRecord:
        async with self._worker_lock_async(redis, worker_id):
            worker = await self.get_worker_async(redis, worker_id)
            if worker is None:
                raise WorkerStateNotFoundError(worker_id)
            updated = _worker_with_status(
                worker,
                status,
                now=now,
                unavailable_reason=unavailable_reason,
                unavailable_detail=unavailable_detail,
            )
            state_key = self.keys.worker_state(worker_id)
            await redis.hash_set(
                state_key,
                mapping=redis_serialization.dump_model_hash(updated),
            )
            await redis.expire(state_key, ttl_seconds)
            return updated

    def update_worker_tenancy(
        self,
        worker_id: str,
        *,
        workspace_id: str,
        owner_user_id: str,
        priority: int,
        region: ProductRegion | None = None,
        ttl_seconds: int = DEFAULT_WORKER_STATE_TTL_SECONDS,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord:
        """Restate who a worker belongs to without touching what it is doing.

        Three fields only, all of them the unit's to decide. Re-adding the worker
        would reset its resource version and overwrite the capacity and status a
        running worker is concurrently changing, which is how a reconcile pass
        would hand a busy machine back its idle capacity.
        """

        def write() -> SchedulerWorkerRecord:
            worker = self.get_worker(worker_id)
            if worker is None:
                raise WorkerStateNotFoundError(worker_id)
            updated = worker.model_copy(
                update={
                    "workspace_id": workspace_id,
                    "owner_user_id": owner_user_id,
                    "priority": priority,
                    "region": region,
                    "resource_version": worker.resource_version + 1,
                    "updated_at": now or utc_now(),
                }
            )
            state_key = self.keys.worker_state(worker_id)
            self.redis.hash_set(state_key, mapping=redis_serialization.dump_model_hash(updated))
            self.redis.expire(state_key, ttl_seconds)
            return updated

        return self._with_worker_lock(worker_id, write)

    def toggle_worker_available(
        self,
        worker_id: str,
        *,
        ttl_seconds: int = DEFAULT_WORKER_STATE_TTL_SECONDS,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord:
        return self.update_worker_status(
            worker_id,
            SchedulerWorkerStatus.Available,
            ttl_seconds=ttl_seconds,
            now=now,
            reconcile_capacity=True,
        )

    def disable_worker(
        self,
        worker_id: str,
        *,
        reason: WorkerUnavailableReason,
        detail: str = "",
        ttl_seconds: int = DEFAULT_WORKER_STATE_TTL_SECONDS,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord:
        return self.update_worker_status(
            worker_id,
            SchedulerWorkerStatus.Unavailable,
            unavailable_reason=reason,
            unavailable_detail=detail,
            ttl_seconds=ttl_seconds,
            now=now,
        )

    def preempt_worker_requests(
        self,
        operation: WorkerPreemptionOperation,
        *,
        now: datetime,
    ) -> WorkerPreemptionQueueResult:
        return self._transition_worker_requests(
            operation,
            status=SchedulerWorkerStatus.Unavailable,
            now=now,
        )

    def drain_worker_for_maintenance(
        self,
        operation: WorkerPlannedDrainOperation,
        *,
        now: datetime,
    ) -> WorkerPreemptionQueueResult:
        return self._transition_worker_requests(
            operation,
            status=SchedulerWorkerStatus.Draining,
            now=now,
        )

    def claim_worker_rollout_slot(
        self,
        capacity_owner_id: str,
        worker_id: str,
        target_revision: str,
        *,
        max_unavailable: int,
        now: datetime,
        ttl_seconds: int = 300,
    ) -> bool:
        if max_unavailable <= 0:
            return False
        expires_at = now.timestamp() + max(ttl_seconds, 1)
        key_ttl_seconds = max(ttl_seconds * 2, 2)
        return bool(
            self.redis.eval_scalar(
                CLAIM_WORKER_ROLLOUT_SLOT_SCRIPT,
                2,
                self.keys.worker_rollout_slots(capacity_owner_id),
                self.keys.worker_rollout_revisions(capacity_owner_id),
                worker_id,
                target_revision,
                now.timestamp(),
                expires_at,
                max_unavailable,
                key_ttl_seconds,
            )
        )

    def release_worker_rollout_slot(
        self,
        capacity_owner_id: str,
        worker_id: str,
        target_revision: str,
    ) -> bool:
        return bool(
            self.redis.eval_scalar(
                RELEASE_WORKER_ROLLOUT_SLOT_SCRIPT,
                2,
                self.keys.worker_rollout_slots(capacity_owner_id),
                self.keys.worker_rollout_revisions(capacity_owner_id),
                worker_id,
                target_revision,
            )
        )

    def _transition_worker_requests(
        self,
        operation: WorkerPreemptionOperation | WorkerPlannedDrainOperation,
        *,
        status: SchedulerWorkerStatus,
        now: datetime,
    ) -> WorkerPreemptionQueueResult:
        def write() -> WorkerPreemptionQueueResult:
            worker = self.get_worker(operation.worker_id)
            if worker is None:
                raise WorkerStateNotFoundError(operation.worker_id)
            operation_key = self.keys.worker_preemption_operation(
                operation.worker_id,
                operation.operation_id,
            )
            recorded_operation = self.redis.get(operation_key)
            if (
                recorded_operation is not None
                and redis_serialization.redis_text(recorded_operation) == operation.operation_id
            ):
                return WorkerPreemptionQueueResult(worker=worker)
            if (
                worker.resource_version != operation.expected_resource_version
                or worker.capacity_owner_id != operation.capacity_owner_id
                or (operation.machine_id and worker.machine_id != operation.machine_id)
            ):
                raise SchedulerRepositoryError(
                    f"worker {operation.worker_id} interruption session fence is stale"
                )
            payloads = self.redis.hash_get_all(
                self.keys.worker_request_payloads(operation.worker_id)
            )
            requests: list[SchedulerWorkerRequest] = []
            # Queued and in-flight alike: an unavailable worker keeps neither.
            # The script decides which requests have not started and may move.
            for list_key in (
                self.keys.worker_requests(operation.worker_id),
                self.keys.worker_inflight_requests(operation.worker_id),
            ):
                for raw_request_id in self.redis.list_range(list_key, 0, -1):
                    request_id = redis_serialization.redis_text(raw_request_id)
                    raw = payloads.get(request_id)
                    if raw is None:
                        continue
                    requests.append(
                        SchedulerWorkerRequest.model_validate_json(
                            redis_serialization.redis_text(raw)
                        ).requeued(now=now)
                    )
            cancellation_keys = [
                self.keys.container_cancellation(request.container_id) for request in requests
            ]
            container_state_keys = [
                self.keys.container_state(request.container_id) for request in requests
            ]
            request_args = [
                value
                for request in requests
                for value in (request.container_id, request.model_dump_json())
            ]
            current_worker_fields = redis_serialization.dump_model_hash(worker)
            updated_worker_fields = redis_serialization.dump_model_hash(
                worker.model_copy(
                    update={
                        "status": status,
                        "resource_version": worker.resource_version + 1,
                        "updated_at": now,
                    }
                )
            )
            result = _redis_script_text_items(
                self.redis.eval_scalars(
                    PREEMPT_WORKER_REQUESTS_SCRIPT,
                    7 + len(cancellation_keys) + len(container_state_keys),
                    self.keys.worker_state(operation.worker_id),
                    self.keys.worker_requests(operation.worker_id),
                    self.keys.worker_request_payloads(operation.worker_id),
                    self.keys.container_requests(),
                    self.keys.container_request_payloads(),
                    operation_key,
                    self.keys.worker_inflight_requests(operation.worker_id),
                    *cancellation_keys,
                    *container_state_keys,
                    str(operation.expected_resource_version),
                    operation.operation_id,
                    len(requests),
                    updated_worker_fields["status"],
                    updated_worker_fields["resource_version"],
                    updated_worker_fields["updated_at"],
                    current_worker_fields["capacity_owner_id"],
                    current_worker_fields["machine_id"] if operation.machine_id else "",
                    now.timestamp(),
                    DEFAULT_CONTAINER_CANCELLATION_TTL_SECONDS,
                    redis_serialization.dumps_field(SchedulerContainerStatus.Pending.value),
                    *request_args,
                )
            )
            result_code = int(result[0]) if result else 0
            if result_code == -1:
                raise WorkerStateNotFoundError(operation.worker_id)
            if result_code == -2:
                raise SchedulerRepositoryError(
                    f"worker {operation.worker_id} interruption session fence is stale"
                )
            updated = self.get_worker(operation.worker_id)
            if updated is None:
                raise WorkerStateNotFoundError(operation.worker_id)
            return WorkerPreemptionQueueResult(
                worker=updated,
                changed=result_code == 1,
                requeued_request_ids=result[1:] if result_code == 1 else [],
            )

        return self._with_worker_lock(operation.worker_id, write)

    def set_keep_alive(
        self,
        worker_id: str,
        *,
        ttl_seconds: int = DEFAULT_WORKER_STATE_TTL_SECONDS,
    ) -> SchedulerWorkerRecord:
        def write() -> SchedulerWorkerRecord:
            worker = self.get_worker(worker_id)
            if worker is None:
                raise WorkerStateNotFoundError(worker_id)
            reconciled = self._reconciled_worker_capacity(worker)
            capacity_changed = _worker_capacity_changed(worker, reconciled)
            state_key = self.keys.worker_state(worker_id)
            if capacity_changed:
                updated = reconciled.model_copy(
                    update={
                        "status": worker.status,
                        "resource_version": worker.resource_version + 1,
                        "updated_at": utc_now(),
                    }
                )
                self.redis.hash_set(
                    state_key,
                    mapping=redis_serialization.dump_model_hash(updated),
                )
                self.redis.expire(state_key, ttl_seconds)
                return updated
            self.redis.expire(state_key, ttl_seconds)
            return worker

        return self._with_worker_lock(worker_id, write)

    def update_worker_capacity(
        self,
        worker_id: str,
        request: SchedulerWorkerRequest,
        change: WorkerCapacityChange,
    ) -> WorkerCapacityPlan:
        def write() -> WorkerCapacityPlan:
            worker = self.get_worker(worker_id)
            if worker is None:
                raise WorkerStateNotFoundError(worker_id)
            plan = plan_worker_capacity_change(worker, request, change)
            if not plan.accepted:
                raise SchedulerRepositoryError(plan.reason)
            self.redis.hash_set(
                self.keys.worker_state(worker_id),
                mapping=redis_serialization.dump_model_hash(plan.worker),
            )
            return plan

        return self._with_worker_lock(worker_id, write)

    def release_worker_capacity(
        self,
        worker_id: str,
        request: WorkerExecutionRequest,
    ) -> WorkerCapacityResult:
        def write() -> WorkerCapacityResult:
            worker = self.get_worker(worker_id)
            if worker is None:
                raise WorkerStateNotFoundError(worker_id)
            restored = _restored_worker_capacity(
                worker,
                cpu_millicores=request.cpu_millicores,
                memory_mib=capacity_memory_mib(request.memory_mib),
                gpu_count=gpu_count_for_capacity(request.gpu, request.gpu_count),
            )
            self.redis.hash_set(
                self.keys.worker_state(worker_id),
                mapping=redis_serialization.dump_model_hash(restored),
            )
            return WorkerCapacityResult(
                worker=restored,
                request=request,
                change=WorkerCapacityChange.Add,
                accepted=True,
            )

        return self._with_worker_lock(worker_id, write)

    def schedule_container_request(
        self,
        worker_id: str,
        request: SchedulerWorkerRequest,
        *,
        reserved_capacity: WorkerReservedCapacity | None = None,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord:
        if request.backfill:
            raise SchedulerRepositoryError("backfill requires an atomic claimed dispatch")

        def write() -> SchedulerWorkerRecord:
            if self.is_container_cancelled(request.container_id):
                msg = f"container request {request.container_id} was cancelled"
                raise SchedulerRepositoryError(msg)
            worker = self.get_worker(worker_id)
            if worker is None:
                raise WorkerStateNotFoundError(worker_id)
            if worker.status is not SchedulerWorkerStatus.Available:
                msg = f"worker {worker_id} is not available"
                raise SchedulerRepositoryError(msg)

            queued_request = request.model_copy(update={"timestamp": now or utc_now()})
            plan = plan_worker_capacity_change(
                worker,
                queued_request,
                WorkerCapacityChange.Remove,
                reserved_capacity=reserved_capacity,
            )
            if not plan.accepted:
                raise SchedulerRepositoryError(plan.reason)

            state_key = self.keys.worker_state(worker_id)
            self.redis.hash_set(state_key, mapping=redis_serialization.dump_model_hash(plan.worker))
            try:
                queued = self._enqueue_worker_request(worker_id, queued_request)
                if not queued:
                    msg = f"container request {request.container_id} was cancelled"
                    raise SchedulerRepositoryError(msg)
            except Exception:
                rollback = plan_worker_capacity_change(
                    plan.worker,
                    queued_request,
                    WorkerCapacityChange.Add,
                    reserved_capacity=reserved_capacity,
                )
                self.redis.hash_set(
                    state_key, mapping=redis_serialization.dump_model_hash(rollback.worker)
                )
                raise
            return plan.worker

        return self._with_worker_lock(worker_id, write)

    def dispatch_claimed_container_request(
        self,
        worker_id: str,
        claim: SchedulerContainerRequestClaim,
        *,
        reserved_capacity: WorkerReservedCapacity | None = None,
        capacity_allocation: CapacityReservationDispatchAllocation | None = None,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord:
        request = claim.request

        def write() -> SchedulerWorkerRecord:
            worker = self.get_worker(worker_id)
            if worker is None:
                raise WorkerStateNotFoundError(worker_id)
            if worker.status is not SchedulerWorkerStatus.Available:
                msg = f"worker {worker_id} is not available"
                raise SchedulerRepositoryError(msg)

            queued_request = request.model_copy(update={"timestamp": now or utc_now()})
            plan = plan_worker_capacity_change(
                worker,
                queued_request,
                WorkerCapacityChange.Remove,
                reserved_capacity=reserved_capacity,
            )
            if not plan.accepted:
                raise SchedulerRepositoryError(plan.reason)
            self._commit_claimed_worker_request(
                worker_id,
                claim,
                queued_request,
                current_worker=worker,
                updated_worker=plan.worker,
                capacity_allocation=capacity_allocation,
            )
            return plan.worker

        return self._with_worker_lock(worker_id, write)

    def _commit_claimed_worker_request(
        self,
        worker_id: str,
        claim: SchedulerContainerRequestClaim,
        queued_request: SchedulerWorkerRequest,
        *,
        current_worker: SchedulerWorkerRecord,
        updated_worker: SchedulerWorkerRecord,
        capacity_allocation: CapacityReservationDispatchAllocation | None,
    ) -> None:
        request_id = claim.request.container_id
        if queued_request.container_id != request_id:
            raise SchedulerRepositoryError("scheduler request identity cannot change while claimed")
        worker_state_key = self.keys.worker_state(worker_id)
        worker_queue_key = self.keys.worker_requests(worker_id)
        worker_payloads_key = self.keys.worker_request_payloads(worker_id)
        worker_inflight_key = self.keys.worker_inflight_requests(worker_id)
        cancellation_key = self.keys.container_cancellation(request_id)
        ready_key = self.keys.container_requests()
        backlog_payloads_key = self.keys.container_request_payloads()
        claims_key = self.keys.container_request_claims()
        claim_owners_key = self.keys.container_request_claim_owners()
        current_worker_fields = redis_serialization.dump_model_hash(current_worker)
        updated_worker_fields = redis_serialization.dump_model_hash(updated_worker)
        worker_field_args = [
            value for field, value in updated_worker_fields.items() for value in (field, value)
        ]
        request_index_key = (
            capacity_allocation.request_index_key
            if capacity_allocation is not None
            else worker_state_key
        )
        allocation_key = (
            capacity_allocation.allocation_key
            if capacity_allocation is not None
            else worker_state_key
        )
        allocation_index_key = (
            capacity_allocation.allocation_index_key
            if capacity_allocation is not None
            else worker_state_key
        )
        # Unknown labels introduced after this snapshot block backfill, so the
        # atomic guard cannot miss a GPU arrival or disagree about an alias.
        gpu_matches = (
            {
                gpu: gpu_preference_accepts([gpu], current_worker.gpu_type)
                for request in self.pending_gpu_requests()
                for gpu in request.gpu
            }
            if queued_request.backfill
            else {}
        )
        result = self.redis.eval_int(
            DISPATCH_CLAIMED_WORKER_REQUEST_SCRIPT,
            14,
            worker_state_key,
            worker_queue_key,
            worker_payloads_key,
            cancellation_key,
            ready_key,
            backlog_payloads_key,
            claims_key,
            claim_owners_key,
            request_index_key,
            allocation_key,
            allocation_index_key,
            worker_inflight_key,
            self.keys.worker_backfill_recovery(worker_id),
            self.keys.container_state(request_id),
            request_id,
            claim.token,
            queued_request.model_dump_json(),
            current_worker_fields["resource_version"],
            "1" if capacity_allocation is not None else "0",
            capacity_allocation.reservation_id if capacity_allocation is not None else "",
            json.dumps(gpu_matches),
            *worker_field_args,
        )
        if result == -2:
            raise ContainerRequestCancelledError(f"container request {request_id} was cancelled")
        if result == -1:
            raise ContainerRequestClaimNotOwnedError(
                f"scheduler request claim is no longer owned: {request_id}"
            )
        if result == -3:
            raise SchedulerRepositoryError(f"worker {worker_id} changed during dispatch")
        if result == -4:
            raise SchedulerRepositoryError(
                f"capacity reservation allocation changed during dispatch: {request_id}"
            )
        if result == -5:
            raise SchedulerRepositoryError("GPU demand or recovery prevents CPU backfill")
        if result != 1:
            raise SchedulerRepositoryError(
                f"scheduler request dispatch returned unexpected status {result}: {request_id}"
            )

    def pending_gpu_requests(self) -> list[SchedulerWorkerRequest]:
        return [
            request
            for payload in self.redis.hash_get_all(self.keys.container_request_payloads()).values()
            if (
                request := SchedulerWorkerRequest.model_validate_json(
                    redis_serialization.redis_text(payload)
                )
            ).gpu
        ]

    def mark_gpu_backfill_evictions(
        self,
        worker: SchedulerWorkerRecord,
        gpu_request_id: str,
        container_ids: list[str],
        *,
        now: datetime | None = None,
    ) -> list[str]:
        fields = redis_serialization.dump_model_hash(worker)
        result = _redis_script_text_items(
            self.redis.eval_scalars(
                PREEMPT_GPU_BACKFILL_SCRIPT,
                3 + len(container_ids),
                self.keys.worker_state(worker.worker_id),
                self.keys.worker_backfill_recovery(worker.worker_id),
                self.keys.container_request_payloads(),
                *(self.keys.container_state(container_id) for container_id in container_ids),
                fields["resource_version"],
                gpu_request_id,
                fields["worker_id"],
                (now or utc_now()).timestamp(),
            )
        )
        if not result or result[0] != "ok":
            return []
        return result[1:]

    async def enqueue_worker_request(
        self,
        redis: AsyncRedisClient,
        worker_id: str,
        request: SchedulerWorkerRequest,
    ) -> int:
        async with self._worker_lock_async(redis, worker_id):
            return await redis.eval_int(
                PLACE_WORKER_REQUEST_SCRIPT,
                4,
                *self._worker_request_placement_keys(worker_id, request, delivered=False),
                request.container_id,
                request.model_dump_json(),
            )

    def _enqueue_worker_request(
        self,
        worker_id: str,
        request: SchedulerWorkerRequest,
    ) -> int:
        return self._place_worker_request(worker_id, request, delivered=False)

    def _place_worker_request(
        self,
        worker_id: str,
        request: SchedulerWorkerRequest,
        *,
        delivered: bool,
    ) -> int:
        """Put this request on exactly one of the worker's two lists.

        Queued and in flight are the same request in two states, so placing it on
        one clears it from the other. Returning a request the worker refused, and
        restoring a drain that failed part-way, both depend on that: leaving the
        old entry behind would let one request be delivered twice from two places.
        """

        return self.redis.eval_int(
            PLACE_WORKER_REQUEST_SCRIPT,
            4,
            *self._worker_request_placement_keys(worker_id, request, delivered=delivered),
            request.container_id,
            request.model_dump_json(),
        )

    def _worker_request_placement_keys(
        self,
        worker_id: str,
        request: SchedulerWorkerRequest,
        *,
        delivered: bool,
    ) -> tuple[str, str, str, str]:
        """Keys for `PLACE_WORKER_REQUEST_SCRIPT`.

        The list the request lands on, the payload hash, the cancellation marker,
        and the list the request is removed from so it is never on both.
        """
        queue_key = self.keys.worker_requests(worker_id)
        inflight_key = self.keys.worker_inflight_requests(worker_id)
        return (
            inflight_key if delivered else queue_key,
            self.keys.worker_request_payloads(worker_id),
            self.keys.container_cancellation(request.container_id),
            queue_key if delivered else inflight_key,
        )

    def enqueue_container_request(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> int:
        scheduled_at = ready_at or utc_now()
        requests_key = self.keys.container_requests()
        payloads_key = self.keys.container_request_payloads()
        cancellation_key = self.keys.container_cancellation(request.container_id)
        payload = request.model_dump_json()
        return self.redis.eval_int(
            ENQUEUE_CONTAINER_REQUEST_SCRIPT,
            4,
            requests_key,
            payloads_key,
            cancellation_key,
            self.keys.container_state(request.container_id),
            request.container_id,
            payload,
            scheduled_at.timestamp(),
        )

    def claim_ready_container_requests(
        self,
        *,
        now: datetime | None = None,
        limit: int = 1,
        lease_seconds: float = DEFAULT_CONTAINER_REQUEST_CLAIM_LEASE_SECONDS,
    ) -> list[SchedulerContainerRequestClaim]:
        if limit <= 0:
            return []
        current_time = now or utc_now()
        score = current_time.timestamp()
        lease_until = score + max(lease_seconds, 0.0)
        claim_token = token_urlsafe(24)
        raw_items = _redis_script_text_items(
            self.redis.eval_scalars(
                CLAIM_READY_CONTAINER_REQUESTS_SCRIPT,
                4,
                self.keys.container_requests(),
                self.keys.container_request_payloads(),
                self.keys.container_request_claims(),
                self.keys.container_request_claim_owners(),
                score,
                limit,
                claim_token,
                lease_until,
            )
        )
        if len(raw_items) % 2 != 0:
            msg = "scheduler request claim returned an invalid response"
            raise SchedulerRepositoryError(msg)
        claims: list[SchedulerContainerRequestClaim] = []
        for index in range(0, len(raw_items), 2):
            request_id = raw_items[index]
            request = SchedulerWorkerRequest.model_validate_json(raw_items[index + 1])
            if request.container_id != request_id:
                msg = f"scheduler request payload identity mismatch: {request_id}"
                raise SchedulerRepositoryError(msg)
            claims.append(
                SchedulerContainerRequestClaim(
                    request=request,
                    token=claim_token,
                )
            )
        return claims

    def acknowledge_container_request(self, claim: SchedulerContainerRequestClaim) -> bool:
        request_id = claim.request.container_id
        return bool(
            self.redis.eval_int(
                ACKNOWLEDGE_CONTAINER_REQUEST_SCRIPT,
                3,
                self.keys.container_request_claims(),
                self.keys.container_request_claim_owners(),
                self.keys.container_request_payloads(),
                request_id,
                claim.token,
            )
        )

    def requeue_container_request(
        self,
        claim: SchedulerContainerRequestClaim,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime,
    ) -> bool:
        request_id = claim.request.container_id
        if request.container_id != request_id:
            raise SchedulerRepositoryError("scheduler request identity cannot change while claimed")
        result = self.redis.eval_int(
            REQUEUE_CONTAINER_REQUEST_SCRIPT,
            5,
            self.keys.container_requests(),
            self.keys.container_request_payloads(),
            self.keys.container_request_claims(),
            self.keys.container_request_claim_owners(),
            self.keys.container_cancellation(request_id),
            request_id,
            claim.token,
            request.model_dump_json(),
            ready_at.timestamp(),
        )
        if result < 0:
            raise SchedulerRepositoryError(
                f"scheduler request claim is no longer owned: {request_id}"
            )
        return bool(result)

    def has_recoverable_container_request(
        self,
        container_id: str,
        *,
        worker_id: str = "",
    ) -> bool:
        """Whether anything is still going to hand this container to a worker.

        The worker payload hash answers for both of the worker's lists, because a
        delivery moves the id and never the payload: a request in flight is one
        the worker has been handed and has not acknowledged, and the callers that
        reap a container the scheduler no longer backs must see it exactly as they
        see one still queued.
        """

        if self.redis.hash_get(self.keys.container_request_payloads(), container_id) is not None:
            return True
        if not worker_id:
            return False
        return (
            self.redis.hash_get(self.keys.worker_request_payloads(worker_id), container_id)
            is not None
        )

    async def _take_worker_request(
        self,
        redis: AsyncRedisClient,
        worker_id: str,
    ) -> str | None:
        result = _redis_script_text_items(
            await redis.eval_scalars(
                TAKE_WORKER_REQUEST_SCRIPT,
                3,
                self.keys.worker_requests(worker_id),
                self.keys.worker_request_payloads(worker_id),
                self.keys.worker_inflight_requests(worker_id),
            )
        )
        return result[1] if len(result) == 2 else None

    async def _claim_moved_worker_request(
        self,
        redis: AsyncRedisClient,
        worker_id: str,
        request_id: str,
    ) -> str:
        value = await redis.eval_scalar(
            CLAIM_MOVED_WORKER_REQUEST_SCRIPT,
            2,
            self.keys.worker_request_payloads(worker_id),
            self.keys.worker_inflight_requests(worker_id),
            request_id,
        )
        if value is None:
            raise SchedulerRepositoryError("worker request take returned no payload")
        return redis_serialization.redis_text(value)

    async def wait_for_next_container_request(
        self,
        redis: AsyncRedisClient,
        worker_id: str,
        *,
        timeout_seconds: float,
    ) -> SchedulerWorkerRequest | None:
        """Hand the worker its next request without destroying it.

        Delivery is at least once. The request moves to the worker's in-flight
        list in the same command that takes it off the queue, and stays there
        until the worker acknowledges it, so a control plane that dies between
        the take and the response redelivers rather than losing the container.
        An unacknowledged request is returned again ahead of the queue, which is
        why the worker has to reconcile a redelivery against what it is already
        running instead of starting a second container.
        """
        if timeout_seconds <= 0:
            raise ValueError("worker request wait timeout must be greater than zero")
        deadline = time.monotonic() + timeout_seconds
        queue_key = self.keys.worker_requests(worker_id)
        inflight_key = self.keys.worker_inflight_requests(worker_id)
        while True:
            raw = await self._take_worker_request(redis, worker_id)
            if raw is not None:
                return SchedulerWorkerRequest.model_validate_json(raw)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            # Redis waits in whole seconds and reads zero as forever, so a shorter
            # remainder is rounded up rather than turned into an unbounded wait.
            moved = await redis.blocking_list_move(
                queue_key,
                inflight_key,
                timeout_seconds=ceil(remaining),
            )
            if moved is None:
                return None
            request_id = redis_serialization.redis_text(moved)
            payload = await self._claim_moved_worker_request(
                redis,
                worker_id,
                request_id,
            )
            if payload:
                return SchedulerWorkerRequest.model_validate_json(payload)

    async def acknowledge_worker_request(
        self,
        redis: AsyncRedisClient,
        worker_id: str,
        container_id: str,
    ) -> bool:
        """Retire a delivered request once the worker has taken the container.

        Until this lands the request is redeliverable, and after it lands nothing
        in Redis holds the container: the durable row's start deadline is what
        covers a worker that acknowledged and then died before starting.
        """

        return bool(
            await redis.eval_int(
                ACKNOWLEDGE_WORKER_REQUEST_SCRIPT,
                2,
                self.keys.worker_inflight_requests(worker_id),
                self.keys.worker_request_payloads(worker_id),
                container_id,
            )
        )

    async def return_worker_request(
        self,
        redis: AsyncRedisClient,
        worker_id: str,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> int:
        """Take a request back off a worker and return it to the ready queue.

        One command for both halves. Acknowledging and then enqueueing would leave
        a window where the request belongs to nobody, which is the same hole as
        popping and then answering.
        """

        scheduled_at = ready_at or utc_now()
        return await redis.eval_int(
            RETURN_WORKER_REQUEST_SCRIPT,
            6,
            self.keys.worker_requests(worker_id),
            self.keys.worker_inflight_requests(worker_id),
            self.keys.worker_request_payloads(worker_id),
            self.keys.container_requests(),
            self.keys.container_request_payloads(),
            self.keys.container_cancellation(request.container_id),
            request.container_id,
            request.model_dump_json(),
            scheduled_at.timestamp(),
        )

    def cancel_worker_request(self, worker_id: str, container_id: str) -> bool:
        def write() -> bool:
            queue_key = self.keys.worker_requests(worker_id)
            payloads_key = self.keys.worker_request_payloads(worker_id)
            inflight_key = self.keys.worker_inflight_requests(worker_id)
            result = _redis_script_text_items(
                self.redis.eval_scalars(
                    CANCEL_WORKER_REQUEST_SCRIPT,
                    3,
                    queue_key,
                    payloads_key,
                    inflight_key,
                    container_id,
                )
            )
            removed = bool(result and int(result[0]) > 0)
            if not removed:
                return False
            worker = self.get_worker(worker_id)
            if worker is not None:
                reconciled = self._reconciled_worker_capacity(worker)
                self.redis.hash_set(
                    self.keys.worker_state(worker_id),
                    mapping=redis_serialization.dump_model_hash(reconciled),
                )
            return True

        return self._with_worker_lock(worker_id, write)

    def drain_worker_requests(self, worker_id: str) -> WorkerRequestDrain:
        queue_key = self.keys.worker_requests(worker_id)
        payloads_key = self.keys.worker_request_payloads(worker_id)
        inflight_key = self.keys.worker_inflight_requests(worker_id)
        drained = _redis_script_text_items(
            self.redis.eval_scalars(
                DRAIN_WORKER_REQUESTS_SCRIPT,
                3,
                queue_key,
                payloads_key,
                inflight_key,
            )
        )
        if not drained:
            return WorkerRequestDrain()
        queued_count = int(drained[0])
        return WorkerRequestDrain(
            queued=drained[1 : 1 + queued_count],
            delivered=drained[1 + queued_count :],
        )

    def restore_worker_requests(self, worker_id: str, drain: WorkerRequestDrain) -> int:
        restored = 0
        for raw_requests, delivered in ((drain.queued, False), (drain.delivered, True)):
            for raw in raw_requests:
                request = SchedulerWorkerRequest.model_validate_json(raw)
                if self.is_container_cancelled(request.container_id):
                    continue
                restored += self._place_worker_request(worker_id, request, delivered=delivered)
        return restored

    def _requeue_drained_worker_requests(
        self,
        worker_id: str,
        drain: WorkerRequestDrain,
        *,
        now: datetime,
    ) -> list[str]:
        """Return a gone worker's requests to the ready queue.

        Only still-pending assignments owned by this worker are cleared. A
        redelivered request may name work already running, finished, or assigned
        elsewhere. The assignment fence and requeue are one Redis operation.
        """

        request_ids: list[str] = []
        for raw_requests in (drain.queued, drain.delivered):
            for raw in raw_requests:
                request = SchedulerWorkerRequest.model_validate_json(raw).requeued(now=now)
                requeued = self.redis.eval_int(
                    REQUEUE_DRAINED_WORKER_REQUEST_SCRIPT,
                    5,
                    self.keys.container_requests(),
                    self.keys.container_request_payloads(),
                    self.keys.container_cancellation(request.container_id),
                    self.keys.container_state(request.container_id),
                    self.keys.container_worker_index(worker_id),
                    request.container_id,
                    request.model_dump_json(),
                    now.timestamp(),
                    worker_id,
                )
                if requeued:
                    request_ids.append(request.container_id)
        return request_ids

    def is_container_cancelled(self, container_id: str) -> bool:
        return bool(self.redis.exists(self.keys.container_cancellation(container_id)))

    def remove_worker(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> WorkerRemovalResult:
        def write() -> WorkerRemovalResult:
            state_key = self.keys.worker_state(worker_id)
            if self._get_worker_from_key(state_key) is None:
                raise WorkerStateNotFoundError(worker_id)

            requeue_time = now or utc_now()
            drain = self.drain_worker_requests(worker_id)
            try:
                request_ids = self._requeue_drained_worker_requests(
                    worker_id, drain, now=requeue_time
                )
            except Exception:
                self.restore_worker_requests(worker_id, drain)
                raise

            self.redis.set_remove(self.keys.worker_index(), state_key)
            self.redis.delete(state_key, self.keys.worker_backfill_recovery(worker_id))
            return WorkerRemovalResult(
                worker_id=worker_id,
                removed=True,
                requeued_count=len(request_ids),
                request_ids=request_ids,
            )

        return self._with_worker_lock(worker_id, write)

    def add_container_to_worker(self, worker_id: str, container_id: str) -> int:
        return int(
            self.redis.set_add(
                self.keys.container_worker_index(worker_id),
                self.keys.container_state(container_id),
            )
        )

    def remove_container_from_worker(self, worker_id: str, container_id: str) -> int:
        return int(
            self.redis.set_remove(
                self.keys.container_worker_index(worker_id),
                self.keys.container_state(container_id),
            )
        )

    def set_image_pull_lock(
        self,
        worker_id: str,
        image_id: str,
        *,
        ttl_seconds: int = DEFAULT_IMAGE_PULL_LOCK_TTL_SECONDS,
        retries: int = DEFAULT_IMAGE_PULL_LOCK_RETRIES,
    ) -> WorkerRepositoryLockRecord:
        key = self.keys.image_pull_lock(worker_id, image_id)
        return _acquire_token_lock(
            self.redis,
            key,
            kind=WorkerRepositoryLockKind.ImagePull,
            owner_id=worker_id,
            resource_id=image_id,
            ttl_seconds=ttl_seconds,
            retries=retries,
        )

    def remove_image_pull_lock(
        self,
        worker_id: str,
        image_id: str,
        token: str,
    ) -> WorkerRepositoryLockRelease:
        key = self.keys.image_pull_lock(worker_id, image_id)
        return _release_token_lock(
            self.redis,
            key,
            token,
            kind=WorkerRepositoryLockKind.ImagePull,
        )

    def reconcile_worker_capacity(self, worker_id: str) -> SchedulerWorkerRecord:
        def write() -> SchedulerWorkerRecord:
            worker = self.get_worker(worker_id)
            if worker is None:
                raise WorkerStateNotFoundError(worker_id)
            updated = self._reconciled_worker_capacity(worker)
            self.redis.hash_set(
                self.keys.worker_state(worker_id),
                mapping=redis_serialization.dump_model_hash(updated),
            )
            return updated

        return self._with_worker_lock(worker_id, write)

    def _with_worker_lock[T](self, worker_id: str, callback: Callable[[], T]) -> T:
        lock = _acquire_token_lock(
            self.redis,
            self.keys.worker_lock(worker_id),
            kind=WorkerRepositoryLockKind.Worker,
            owner_id=worker_id,
            resource_id=worker_id,
            ttl_seconds=DEFAULT_WORKER_LOCK_TTL_SECONDS,
            retries=DEFAULT_WORKER_LOCK_RETRIES,
        )
        if not lock.acquired:
            msg = f"worker {worker_id} lock not acquired"
            raise SchedulerRepositoryError(msg)
        try:
            return callback()
        finally:
            _release_token_lock(
                self.redis,
                lock.key,
                lock.token,
                kind=WorkerRepositoryLockKind.Worker,
            )

    @asynccontextmanager
    async def _worker_lock_async(
        self,
        redis: AsyncRedisClient,
        worker_id: str,
    ) -> AsyncIterator[None]:
        lock = await _acquire_token_lock_async(
            redis,
            self.keys.worker_lock(worker_id),
            kind=WorkerRepositoryLockKind.Worker,
            owner_id=worker_id,
            resource_id=worker_id,
            ttl_seconds=DEFAULT_WORKER_LOCK_TTL_SECONDS,
            retries=DEFAULT_WORKER_LOCK_RETRIES,
        )
        if not lock.acquired:
            msg = f"worker {worker_id} lock not acquired"
            raise SchedulerRepositoryError(msg)
        try:
            yield
        finally:
            await _release_token_lock_async(
                redis,
                lock.key,
                lock.token,
                kind=WorkerRepositoryLockKind.Worker,
            )

    def _reconciled_worker_capacity(
        self,
        worker: SchedulerWorkerRecord,
    ) -> SchedulerWorkerRecord:
        reserved = self._worker_reserved_capacity(worker.worker_id)
        updates: dict[str, int] = {}
        if worker.total_cpu_millicores > 0:
            updates["free_cpu_millicores"] = max(
                worker.total_cpu_millicores - reserved.cpu_millicores,
                0,
            )
        if worker.total_memory_mib > 0:
            updates["free_memory_mib"] = max(worker.total_memory_mib - reserved.memory_mib, 0)
        if worker.total_gpu_count > 0:
            updates["free_gpu_count"] = max(worker.total_gpu_count - reserved.gpu_count, 0)
        if not updates:
            return worker
        return worker.model_copy(update=updates)

    def _worker_reserved_capacity(self, worker_id: str) -> WorkerReservedCapacity:
        reserved = WorkerReservedCapacity()
        payloads = self.redis.hash_get_all(self.keys.worker_request_payloads(worker_id))
        for raw_request_id in self.redis.list_range(
            self.keys.worker_requests(worker_id),
            0,
            -1,
        ):
            request_id = redis_serialization.redis_text(raw_request_id)
            raw_request = payloads.get(request_id)
            if raw_request is None:
                continue
            request = SchedulerWorkerRequest.model_validate_json(
                redis_serialization.redis_text(raw_request)
            )
            reserved.cpu_millicores += request.cpu_millicores
            reserved.memory_mib += capacity_memory_mib(request.memory_mib)
            reserved.gpu_count += gpu_count_for_capacity(request.gpu, request.gpu_count)

        index_key = self.keys.container_worker_index(worker_id)
        for state_key in sorted(
            redis_serialization.redis_strings(self.redis.set_members(index_key))
        ):
            raw = self.redis.hash_get_all(state_key)
            if not raw:
                self.redis.set_remove(index_key, state_key)
                continue
            state = redis_serialization.load_model_hash(SchedulerContainerState, raw)
            if state.status not in {
                SchedulerContainerStatus.Pending,
                SchedulerContainerStatus.Running,
            }:
                continue
            reserved.cpu_millicores += state.cpu_millicores
            reserved.memory_mib += capacity_memory_mib(state.memory_mib)
            # The state's count is already resolved, so it is added as it
            # stands. Put back through the request-side helper it answered zero
            # for any state whose card was not recorded, and a worker's reserved
            # GPUs were undercounted by exactly the ones nobody had named.
            reserved.gpu_count += state.gpu_count
        return reserved

    def _get_worker_from_key(self, key: str) -> SchedulerWorkerRecord | None:
        raw = self.redis.hash_get_all(key)
        if not raw:
            return None
        return redis_serialization.load_model_hash(SchedulerWorkerRecord, raw)


@dataclass(init=False, slots=True)
class RedisSchedulerContainerRepository:
    redis: RedisClient
    keys: SchedulerStateKeys

    def __init__(self, redis: RedisClient, keys: SchedulerStateKeys | None = None) -> None:
        self.redis = redis
        self.keys = keys or SchedulerStateKeys(redis)

    def set_container_state(
        self,
        state: SchedulerContainerState,
        *,
        ttl_seconds: int = DEFAULT_CONTAINER_STATE_TTL_SECONDS,
    ) -> SchedulerContainerState:
        return self._store_container_state(state, ttl_seconds=ttl_seconds, initialize=False)

    def initialize_container_state(self, state: SchedulerContainerState) -> SchedulerContainerState:
        return self._store_container_state(
            state, ttl_seconds=DEFAULT_CONTAINER_STATE_TTL_SECONDS, initialize=True
        )

    def _store_container_state(
        self, state: SchedulerContainerState, *, ttl_seconds: int, initialize: bool
    ) -> SchedulerContainerState:
        def write() -> SchedulerContainerState:
            if self.is_container_cancelled(state.container_id):
                current = self.get_container_state(state.container_id)
                return current or state.model_copy(
                    update={"status": SchedulerContainerStatus.Stopping}
                )
            state_key = self.keys.container_state(state.container_id)
            current = self.get_container_state(state.container_id)
            if initialize and current is not None:
                if current.workspace_id != state.workspace_id or current.stub_id != state.stub_id:
                    raise SchedulerRepositoryError("container submission identity does not match")
                return current
            if current is not None:
                self._remove_container_indexes(current, state_key=state_key)
            self.redis.hash_set(
                state_key,
                mapping=redis_serialization.dump_model_hash(state),
            )
            self.redis.expire(state_key, ttl_seconds)
            self._add_container_indexes(
                state,
                state_key=state_key,
                ttl_seconds=ttl_seconds,
            )
            return state

        return self._with_container_lock(state.container_id, write)

    def get_container_state(self, container_id: str) -> SchedulerContainerState | None:
        raw = self.redis.hash_get_all(self.keys.container_state(container_id))
        if not raw:
            return None
        return redis_serialization.load_model_hash(SchedulerContainerState, raw)

    def container_statuses(
        self,
        container_ids: Sequence[str],
    ) -> dict[str, SchedulerContainerStatus]:
        """What the scheduler currently says about each of these containers.

        One round trip for the whole set, and only the field the answer needs.
        The callers that ask this ask it about every live container of a workload
        on every pass, where reading a whole state each would be a round trip and
        a model validation per container per tick.

        A container with no state is absent rather than carrying a placeholder
        status: "the scheduler no longer backs this" is a different fact from any
        status it could hold, and the callers separate them.
        """

        ids = list(container_ids)
        if not ids:
            return {}
        raw = self.redis.eval_scalars(
            CONTAINER_STATUSES_SCRIPT,
            len(ids),
            *(self.keys.container_state(container_id) for container_id in ids),
        )
        if len(raw) != len(ids):
            msg = "scheduler container status read returned an invalid response"
            raise SchedulerRepositoryError(msg)
        statuses: dict[str, SchedulerContainerStatus] = {}
        for container_id, value in zip(ids, raw, strict=True):
            text = redis_serialization.redis_text(value)
            if not text:
                continue
            statuses[container_id] = SchedulerContainerStatus(
                str(redis_serialization.loads_field(text))
            )
        return statuses

    def is_container_cancelled(self, container_id: str) -> bool:
        return bool(self.redis.exists(self.keys.container_cancellation(container_id)))

    def update_container_status(
        self,
        container_id: str,
        status: SchedulerContainerStatus,
        *,
        ttl_seconds: int = DEFAULT_CONTAINER_STATE_TTL_SECONDS,
        now: datetime | None = None,
    ) -> ContainerStatusUpdatePlan:
        if status not in UPDATABLE_CONTAINER_STATUSES:
            msg = f"invalid scheduler container status: {status}"
            raise ValueError(msg)

        def write() -> tuple[ContainerStatusUpdatePlan, str | None]:
            if status is SchedulerContainerStatus.Stopping:
                self._fence_container_request(container_id)
            state = self.get_container_state(container_id)
            if state is None:
                raise ContainerStateNotFoundError(container_id)

            if state.status is SchedulerContainerStatus.Stopping:
                return (
                    ContainerStatusUpdatePlan(
                        container_id=container_id,
                        previous_status=state.status,
                        next_status=state.status,
                        changed=False,
                        ttl_seconds=ttl_seconds,
                    ),
                    None,
                )

            started_at_set = (
                status is SchedulerContainerStatus.Running
                and state.status is not SchedulerContainerStatus.Running
                and state.started_at is None
            )
            updated = state.model_copy(
                update={
                    "status": status,
                    "started_at": (now or utc_now()) if started_at_set else state.started_at,
                }
            )
            state_key = self.keys.container_state(container_id)
            self.redis.hash_set(
                state_key,
                mapping=redis_serialization.dump_model_hash(updated),
            )
            self.redis.expire(state_key, ttl_seconds)
            self._refresh_container_index_ttls(updated, ttl_seconds=ttl_seconds)
            release_concurrency = status in {
                SchedulerContainerStatus.Stopping,
                SchedulerContainerStatus.Complete,
                SchedulerContainerStatus.Failed,
            }
            return (
                ContainerStatusUpdatePlan(
                    container_id=container_id,
                    previous_status=state.status,
                    next_status=status,
                    changed=state.status is not status or started_at_set,
                    started_at_set=started_at_set,
                    release_concurrency=release_concurrency,
                    ttl_seconds=ttl_seconds,
                ),
                updated.workspace_id if release_concurrency else None,
            )

        plan, release_workspace_id = self._with_container_lock(container_id, write)
        if release_workspace_id is not None:
            self.release_concurrency_reservation(release_workspace_id, container_id)
        return plan

    def cancel_container_request(
        self,
        container_id: str,
        *,
        ttl_seconds: int = DEFAULT_CONTAINER_STATE_TTL_SECONDS,
    ) -> SchedulerContainerState | None:
        def write() -> tuple[SchedulerContainerState | None, str | None]:
            self._fence_container_request(container_id)
            state = self.get_container_state(container_id)
            if state is None:
                return None, None
            if state.status in {
                SchedulerContainerStatus.Complete,
                SchedulerContainerStatus.Failed,
            }:
                return state, None
            updated = state.model_copy(update={"status": SchedulerContainerStatus.Stopping})
            self.redis.hash_set(
                self.keys.container_state(container_id),
                mapping=redis_serialization.dump_model_hash(updated),
            )
            self.redis.expire(self.keys.container_state(container_id), ttl_seconds)
            self._refresh_container_index_ttls(updated, ttl_seconds=ttl_seconds)
            return updated, updated.workspace_id

        state, release_workspace_id = self._with_container_lock(container_id, write)
        if release_workspace_id is not None:
            self.release_concurrency_reservation(release_workspace_id, container_id)
        return state

    def delete_container_state(self, container_id: str) -> bool:
        def write() -> tuple[bool, str | None]:
            self._fence_container_request(container_id)
            state = self.get_container_state(container_id)
            if state is not None:
                self._remove_container_indexes(
                    state,
                    state_key=self.keys.container_state(container_id),
                )
            deleted = bool(self.redis.delete(self.keys.container_state(container_id)))
            self.redis.delete(self.keys.container_address(container_id))
            self.redis.delete(self.keys.container_address_map(container_id))
            self.redis.delete(self.keys.worker_address(container_id))
            return deleted, state.workspace_id if state is not None else None

        deleted, release_workspace_id = self._with_container_lock(container_id, write)
        if release_workspace_id is not None:
            self.release_concurrency_reservation(release_workspace_id, container_id)
        return deleted

    def delete_workspace_container_state(
        self,
        workspace_id: str,
        *,
        container_ids: Iterable[str] = (),
    ) -> int:
        states = self.list_by_workspace(workspace_id)
        indexed_container_ids = sorted(
            redis_serialization.redis_strings(
                self.redis.set_members(self.keys.container_workspace_ownership_index(workspace_id))
            )
        )
        owned_container_ids = tuple(
            dict.fromkeys(
                [
                    *(state.container_id for state in states),
                    *indexed_container_ids,
                    *container_ids,
                ]
            )
        )
        deleted = sum(
            self.delete_container_state(container_id) for container_id in owned_container_ids
        )
        for container_id in owned_container_ids:
            self.redis.delete(self.keys.container_cancellation(container_id))
            self.redis.delete(self.keys.container_exit_code(container_id))
            self.redis.delete(self.keys.container_termination_reason(container_id))
        self.redis.delete(self.keys.container_workspace_index(workspace_id))
        self.redis.delete(self.keys.container_workspace_ownership_index(workspace_id))
        self.redis.delete(self.keys.workspace_concurrency_counter(workspace_id))
        self.redis.delete(self.keys.workspace_concurrency_reservation_index(workspace_id))
        return deleted

    def _fence_container_request(self, container_id: str) -> int:
        requests_key = self.keys.container_requests()
        payloads_key = self.keys.container_request_payloads()
        claims_key = self.keys.container_request_claims()
        claim_owners_key = self.keys.container_request_claim_owners()
        cancellation_key = self.keys.container_cancellation(container_id)
        return self.redis.eval_int(
            FENCE_CONTAINER_REQUEST_SCRIPT,
            5,
            requests_key,
            payloads_key,
            claims_key,
            claim_owners_key,
            cancellation_key,
            container_id,
            DEFAULT_CONTAINER_CANCELLATION_TTL_SECONDS,
        )

    def _add_container_indexes(
        self,
        state: SchedulerContainerState,
        *,
        state_key: str,
        ttl_seconds: int,
    ) -> None:
        index_keys = [
            self.keys.container_stub_index(state.stub_id),
            self.keys.container_workspace_index(state.workspace_id),
        ]
        if state.worker_id:
            index_keys.append(self.keys.container_worker_index(state.worker_id))
        for index_key in index_keys:
            self.redis.set_add(index_key, state_key)
            self.redis.expire(index_key, ttl_seconds)
        ownership_index = self.keys.container_workspace_ownership_index(state.workspace_id)
        self.redis.set_add(ownership_index, state.container_id)
        self.redis.expire(
            ownership_index,
            max(
                ttl_seconds,
                DEFAULT_CONTAINER_CANCELLATION_TTL_SECONDS,
                DEFAULT_CONTAINER_EXIT_CODE_TTL_SECONDS,
            ),
        )

    def _remove_container_indexes(
        self,
        state: SchedulerContainerState,
        *,
        state_key: str,
    ) -> None:
        index_keys = [
            self.keys.container_stub_index(state.stub_id),
            self.keys.container_workspace_index(state.workspace_id),
        ]
        if state.worker_id:
            index_keys.append(self.keys.container_worker_index(state.worker_id))
        for index_key in index_keys:
            self.redis.set_remove(index_key, state_key)
            if int(self.redis.set_cardinality(index_key)) == 0:
                self.redis.delete(index_key)

    def _refresh_container_index_ttls(
        self,
        state: SchedulerContainerState,
        *,
        ttl_seconds: int,
    ) -> None:
        index_keys = [
            self.keys.container_stub_index(state.stub_id),
            self.keys.container_workspace_index(state.workspace_id),
        ]
        if state.worker_id:
            index_keys.append(self.keys.container_worker_index(state.worker_id))
        for index_key in index_keys:
            self.redis.expire(index_key, ttl_seconds)
        self.redis.expire(
            self.keys.container_workspace_ownership_index(state.workspace_id),
            max(
                ttl_seconds,
                DEFAULT_CONTAINER_CANCELLATION_TTL_SECONDS,
                DEFAULT_CONTAINER_EXIT_CODE_TTL_SECONDS,
            ),
        )

    def _with_container_lock[T](self, container_id: str, callback: Callable[[], T]) -> T:
        lock = _acquire_token_lock(
            self.redis,
            self.keys.container_lock(container_id),
            kind=WorkerRepositoryLockKind.Container,
            owner_id=container_id,
            resource_id=container_id,
            ttl_seconds=DEFAULT_CONTAINER_LOCK_TTL_SECONDS,
            retries=DEFAULT_CONTAINER_LOCK_RETRIES,
        )
        if not lock.acquired:
            msg = f"container {container_id} lock not acquired"
            raise SchedulerRepositoryError(msg)
        try:
            return callback()
        finally:
            _release_token_lock(
                self.redis,
                lock.key,
                lock.token,
                kind=WorkerRepositoryLockKind.Container,
            )

    def set_exit_code(
        self,
        container_id: str,
        exit_code: int,
        *,
        termination_reason: StopContainerReason = StopContainerReason.Unknown,
        ttl_seconds: int = DEFAULT_CONTAINER_EXIT_CODE_TTL_SECONDS,
    ) -> None:
        self.redis.set(
            self.keys.container_exit_code(container_id),
            str(exit_code),
            ex=ttl_seconds,
        )
        self.redis.set(
            self.keys.container_termination_reason(container_id),
            termination_reason.value,
            ex=ttl_seconds,
        )

    def get_exit_code(self, container_id: str) -> int | None:
        raw = self.redis.get(self.keys.container_exit_code(container_id))
        if raw is None:
            return None
        return int(redis_serialization.redis_text(raw))

    def get_termination_reason(self, container_id: str) -> StopContainerReason | None:
        raw = self.redis.get(self.keys.container_termination_reason(container_id))
        if raw is None:
            return None
        return StopContainerReason(redis_serialization.redis_text(raw))

    def set_container_address(
        self,
        container_id: str,
        address: str,
        *,
        route: AgentBackendRoute | None = None,
    ) -> SchedulerContainerAddress:
        record = SchedulerContainerAddress(
            container_id=container_id,
            address=address,
            route=_route_for_container(route, container_id=container_id),
        )
        self.redis.set(
            self.keys.container_address(container_id), redis_serialization.dump_model_json(record)
        )
        return record

    def get_container_address(self, container_id: str) -> SchedulerContainerAddress | None:
        raw = self.redis.get(self.keys.container_address(container_id))
        if raw is None:
            return None
        return redis_serialization.load_model_json(SchedulerContainerAddress, raw)

    def set_container_address_map(
        self,
        container_id: str,
        address_map: dict[int, str],
        *,
        routes: list[AgentBackendRoute] | None = None,
    ) -> SchedulerContainerAddressMap:
        route_records: list[AgentBackendRoute] = []
        for route in routes or []:
            route_record = _route_for_container(route, container_id=container_id)
            if route_record is not None:
                route_records.append(route_record)
        record = SchedulerContainerAddressMap(
            container_id=container_id,
            address_map={int(port): address for port, address in address_map.items()},
            routes=route_records,
        )
        self.redis.set(
            self.keys.container_address_map(container_id),
            redis_serialization.dump_model_json(record),
        )
        return record

    def get_container_address_map(self, container_id: str) -> SchedulerContainerAddressMap:
        raw = self.redis.get(self.keys.container_address_map(container_id))
        if raw is None:
            return SchedulerContainerAddressMap(container_id=container_id)
        return redis_serialization.load_model_json(SchedulerContainerAddressMap, raw)

    def set_worker_address(
        self,
        container_id: str,
        address: str,
        *,
        route: AgentBackendRoute | None = None,
    ) -> SchedulerContainerAddress:
        record = SchedulerContainerAddress(
            container_id=container_id,
            address=address,
            route=_route_for_container(route, container_id=container_id),
        )
        self.redis.set(
            self.keys.worker_address(container_id), redis_serialization.dump_model_json(record)
        )
        return record

    def get_worker_address(self, container_id: str) -> SchedulerContainerAddress | None:
        raw = self.redis.get(self.keys.worker_address(container_id))
        if raw is None:
            return None
        return redis_serialization.load_model_json(SchedulerContainerAddress, raw)

    def update_backend_route(self, route: AgentBackendRoute) -> AgentBackendRoute | None:
        route_record = _route_for_container(route, container_id=route.container_id)
        if route_record is None:
            return None
        route = route_record
        changed = False

        address = self.get_container_address(route.container_id)
        if address is not None and _same_route(address.route, route):
            self.set_container_address(route.container_id, address.address, route=route)
            changed = True

        worker_address = self.get_worker_address(route.container_id)
        if worker_address is not None and _same_route(worker_address.route, route):
            self.set_worker_address(route.container_id, worker_address.address, route=route)
            changed = True

        address_map = self.get_container_address_map(route.container_id)
        updated_routes = [
            route if _same_route(existing, route) else existing for existing in address_map.routes
        ]
        if updated_routes != address_map.routes:
            self.set_container_address_map(
                route.container_id,
                address_map.address_map,
                routes=updated_routes,
            )
            changed = True

        return route if changed else None

    def list_by_stub(self, stub_id: str) -> list[SchedulerContainerState]:
        index_key = self.keys.container_stub_index(stub_id)
        return self._list_container_state_by_index(index_key)

    def list_by_workspace(self, workspace_id: str) -> list[SchedulerContainerState]:
        index_key = self.keys.container_workspace_index(workspace_id)
        return self._list_container_state_by_index(index_key)

    def list_by_worker(self, worker_id: str) -> list[SchedulerContainerState]:
        index_key = self.keys.container_worker_index(worker_id)
        return self._list_container_state_by_index(index_key)

    def reserve_concurrency(
        self,
        *,
        workspace_id: str,
        container_id: str,
        workspace_gpu_quota: int,
        workspace_cpu_quota_millicores: int,
        request_gpu_count: int,
        request_cpu_millicores: int,
        now: datetime | None = None,
    ) -> ConcurrencyReservationDecision:
        current_time = now or utc_now()
        self.ensure_workspace_concurrency_counter(workspace_id, now=current_time)
        decision = self._reserve_concurrency_once(
            workspace_id=workspace_id,
            container_id=container_id,
            workspace_gpu_quota=workspace_gpu_quota,
            workspace_cpu_quota_millicores=workspace_cpu_quota_millicores,
            request_gpu_count=request_gpu_count,
            request_cpu_millicores=request_cpu_millicores,
            now=current_time,
        )
        if decision.status is ConcurrencyReservationStatus.Repairing:
            self.ensure_workspace_concurrency_counter(
                workspace_id,
                now=current_time,
                force=True,
            )
            decision = self._reserve_concurrency_once(
                workspace_id=workspace_id,
                container_id=container_id,
                workspace_gpu_quota=workspace_gpu_quota,
                workspace_cpu_quota_millicores=workspace_cpu_quota_millicores,
                request_gpu_count=request_gpu_count,
                request_cpu_millicores=request_cpu_millicores,
                now=current_time,
            )
        if decision.status in {
            ConcurrencyReservationStatus.GpuExceeded,
            ConcurrencyReservationStatus.CpuExceeded,
        } and self.repair_workspace_concurrency_counter_after_throttle(
            workspace_id,
            now=current_time,
        ):
            decision = self._reserve_concurrency_once(
                workspace_id=workspace_id,
                container_id=container_id,
                workspace_gpu_quota=workspace_gpu_quota,
                workspace_cpu_quota_millicores=workspace_cpu_quota_millicores,
                request_gpu_count=request_gpu_count,
                request_cpu_millicores=request_cpu_millicores,
                now=current_time,
            )
        return decision

    def release_concurrency_reservation(
        self,
        workspace_id: str,
        container_id: str,
        *,
        now: datetime | None = None,
    ) -> ConcurrencyReservationDecision:
        if workspace_id == "" or container_id == "":
            return ConcurrencyReservationDecision(
                status=ConcurrencyReservationStatus.Missing,
                counter=ConcurrencyCounter(workspace_id=workspace_id, initialized=False),
            )
        current_time = now or utc_now()
        decision = self._release_concurrency_once(
            workspace_id,
            container_id,
            now=current_time,
        )
        if decision.status is ConcurrencyReservationStatus.Repairing:
            self.ensure_workspace_concurrency_counter(
                workspace_id,
                now=current_time,
                force=True,
            )
            decision = self._release_concurrency_once(
                workspace_id,
                container_id,
                now=current_time,
            )
        return decision

    def ensure_workspace_concurrency_counter(
        self,
        workspace_id: str,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> ConcurrencyCounter:
        if workspace_id == "":
            return ConcurrencyCounter(workspace_id=workspace_id, initialized=False)
        if not force:
            counter = self.get_concurrency_counter(workspace_id)
            if _concurrency_counter_ready(counter):
                return counter

        lock = _acquire_token_lock(
            self.redis,
            self.keys.workspace_concurrency_lock(workspace_id),
            kind=WorkerRepositoryLockKind.WorkspaceConcurrency,
            owner_id=workspace_id,
            resource_id=workspace_id,
            ttl_seconds=DEFAULT_CONCURRENCY_LOCK_TTL_SECONDS,
            retries=0,
        )
        if lock.acquired:
            try:
                if not force:
                    counter = self.get_concurrency_counter(workspace_id)
                    if _concurrency_counter_ready(counter):
                        return counter
                return self.rebuild_workspace_concurrency_counter(workspace_id, now=now)
            finally:
                _release_token_lock(
                    self.redis,
                    lock.key,
                    lock.token,
                    kind=WorkerRepositoryLockKind.WorkspaceConcurrency,
                )
        return self._wait_for_concurrency_counter(workspace_id)

    def rebuild_workspace_concurrency_counter(
        self,
        workspace_id: str,
        *,
        now: datetime | None = None,
    ) -> ConcurrencyCounter:
        current_time = now or utc_now()
        repairing = ConcurrencyCounter(
            workspace_id=workspace_id,
            initialized=False,
            repairing=True,
            updated_at=current_time,
            repair_started_at=current_time,
        )
        self._write_concurrency_counter(repairing)

        states_by_container_id = {
            state.container_id: state for state in self.list_by_workspace(workspace_id)
        }
        active_states = {
            state.container_id: state
            for state in states_by_container_id.values()
            if state.status in {SchedulerContainerStatus.Pending, SchedulerContainerStatus.Running}
        }
        reservation_index = self.keys.workspace_concurrency_reservation_index(workspace_id)
        reservation_ids = sorted(
            redis_serialization.redis_strings(self.redis.set_members(reservation_index))
        )
        active_ids = set(active_states)

        total_gpu = 0
        total_cpu = 0
        for state in active_states.values():
            total_gpu += state.gpu_count
            total_cpu += state.cpu_millicores
            self._write_concurrency_reservation(
                ConcurrencyReservation(
                    workspace_id=workspace_id,
                    container_id=state.container_id,
                    gpu_count=state.gpu_count,
                    cpu_millicores=state.cpu_millicores,
                    created_at=current_time,
                )
            )
            self.redis.set_add(reservation_index, state.container_id)

        for reservation_id in reservation_ids:
            if reservation_id == "" or reservation_id in active_ids:
                if reservation_id == "":
                    self.redis.set_remove(reservation_index, reservation_id)
                continue
            if reservation_id in states_by_container_id:
                self.redis.delete(
                    self.keys.workspace_concurrency_reservation(workspace_id, reservation_id)
                )
                self.redis.set_remove(reservation_index, reservation_id)
                continue
            reservation = self.get_concurrency_reservation(workspace_id, reservation_id)
            if reservation is None or reservation.container_id == "":
                self.redis.delete(
                    self.keys.workspace_concurrency_reservation(workspace_id, reservation_id)
                )
                self.redis.set_remove(reservation_index, reservation_id)
                continue
            if (
                current_time - reservation.created_at
            ).total_seconds() > DEFAULT_CONCURRENCY_RESERVATION_IN_FLIGHT_TTL_SECONDS:
                self.redis.delete(
                    self.keys.workspace_concurrency_reservation(
                        workspace_id,
                        reservation.container_id,
                    )
                )
                self.redis.set_remove(reservation_index, reservation.container_id)
                continue
            total_gpu += reservation.gpu_count
            total_cpu += reservation.cpu_millicores

        counter = ConcurrencyCounter(
            workspace_id=workspace_id,
            initialized=True,
            repairing=False,
            gpu_count=total_gpu,
            cpu_millicores=total_cpu,
            updated_at=current_time,
            repaired_at=current_time,
        )
        self._write_concurrency_counter(counter)
        return counter

    def repair_workspace_concurrency_counter_after_throttle(
        self,
        workspace_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        current_time = now or utc_now()
        counter = self.get_concurrency_counter(workspace_id)
        if not _concurrency_counter_needs_repair(counter, now=current_time):
            return False
        lock = _acquire_token_lock(
            self.redis,
            self.keys.workspace_concurrency_lock(workspace_id),
            kind=WorkerRepositoryLockKind.WorkspaceConcurrency,
            owner_id=workspace_id,
            resource_id=workspace_id,
            ttl_seconds=DEFAULT_CONCURRENCY_LOCK_TTL_SECONDS,
            retries=0,
        )
        if not lock.acquired:
            return False
        try:
            counter = self.get_concurrency_counter(workspace_id)
            if not _concurrency_counter_needs_repair(counter, now=current_time):
                return False
            self.rebuild_workspace_concurrency_counter(workspace_id, now=current_time)
            return True
        finally:
            _release_token_lock(
                self.redis,
                lock.key,
                lock.token,
                kind=WorkerRepositoryLockKind.WorkspaceConcurrency,
            )

    def get_concurrency_counter(self, workspace_id: str) -> ConcurrencyCounter:
        raw = self.redis.hash_get_all(self.keys.workspace_concurrency_counter(workspace_id))
        if not raw:
            return ConcurrencyCounter(workspace_id=workspace_id, initialized=False)
        return redis_serialization.load_model_hash(ConcurrencyCounter, raw)

    def get_concurrency_reservation(
        self,
        workspace_id: str,
        container_id: str,
    ) -> ConcurrencyReservation | None:
        raw = self.redis.hash_get_all(
            self.keys.workspace_concurrency_reservation(workspace_id, container_id)
        )
        if not raw:
            return None
        return redis_serialization.load_model_hash(ConcurrencyReservation, raw)

    def _reserve_concurrency_once(
        self,
        *,
        workspace_id: str,
        container_id: str,
        workspace_gpu_quota: int,
        workspace_cpu_quota_millicores: int,
        request_gpu_count: int,
        request_cpu_millicores: int,
        now: datetime,
    ) -> ConcurrencyReservationDecision:
        reservation = ConcurrencyReservation(
            workspace_id=workspace_id,
            container_id=container_id,
            gpu_count=request_gpu_count,
            cpu_millicores=request_cpu_millicores,
            created_at=now,
        )
        reservation_values = redis_serialization.dump_model_hash(reservation)
        counter_values = redis_serialization.dump_model_hash(
            ConcurrencyCounter(workspace_id=workspace_id)
        )
        raw_result = self.redis.eval_scalars(
            RESERVE_CONCURRENCY_SCRIPT,
            3,
            self.keys.workspace_concurrency_counter(workspace_id),
            self.keys.workspace_concurrency_reservation(workspace_id, container_id),
            self.keys.workspace_concurrency_reservation_index(workspace_id),
            workspace_gpu_quota,
            workspace_cpu_quota_millicores,
            request_gpu_count,
            request_cpu_millicores,
            reservation_values["workspace_id"],
            reservation_values["container_id"],
            container_id,
            reservation_values["created_at"],
            counter_values["initialized"],
            counter_values["repairing"],
            ConcurrencyReservationStatus.Ok.value,
            ConcurrencyReservationStatus.Repairing.value,
            ConcurrencyReservationStatus.GpuExceeded.value,
            ConcurrencyReservationStatus.CpuExceeded.value,
        )
        status_value, changed = _script_status_changed(raw_result)
        status = ConcurrencyReservationStatus(status_value)
        counter = self.get_concurrency_counter(workspace_id)
        stored_reservation = self.get_concurrency_reservation(workspace_id, container_id)
        return ConcurrencyReservationDecision(
            status=status,
            counter=counter,
            reservation=stored_reservation,
            reason=_concurrency_decision_reason(status),
            changed=changed,
        )

    def _release_concurrency_once(
        self,
        workspace_id: str,
        container_id: str,
        *,
        now: datetime,
    ) -> ConcurrencyReservationDecision:
        counter_values = redis_serialization.dump_model_hash(
            ConcurrencyCounter(workspace_id=workspace_id)
        )
        now_value = redis_serialization.dump_model_hash(
            ConcurrencyCounter(workspace_id=workspace_id, updated_at=now)
        )["updated_at"]
        raw_result = self.redis.eval_scalars(
            RELEASE_CONCURRENCY_SCRIPT,
            3,
            self.keys.workspace_concurrency_counter(workspace_id),
            self.keys.workspace_concurrency_reservation(workspace_id, container_id),
            self.keys.workspace_concurrency_reservation_index(workspace_id),
            now_value,
            container_id,
            counter_values["initialized"],
            counter_values["repairing"],
            ConcurrencyReservationStatus.Ok.value,
            ConcurrencyReservationStatus.Missing.value,
            ConcurrencyReservationStatus.Repairing.value,
        )
        status_value, changed = _script_status_changed(raw_result)
        status = ConcurrencyReservationStatus(status_value)
        return ConcurrencyReservationDecision(
            status=status,
            counter=self.get_concurrency_counter(workspace_id),
            reservation=self.get_concurrency_reservation(workspace_id, container_id),
            reason=_concurrency_decision_reason(status),
            changed=changed,
        )

    def _wait_for_concurrency_counter(self, workspace_id: str) -> ConcurrencyCounter:
        deadline = time.monotonic() + DEFAULT_CONCURRENCY_COUNTER_WAIT_SECONDS
        while time.monotonic() < deadline:
            counter = self.get_concurrency_counter(workspace_id)
            if _concurrency_counter_ready(counter):
                return counter
            time.sleep(DEFAULT_CONCURRENCY_COUNTER_POLL_SECONDS)
        msg = f"concurrency counter initialization timed out for workspace {workspace_id}"
        raise SchedulerRepositoryError(msg)

    def _write_concurrency_counter(self, counter: ConcurrencyCounter) -> None:
        self.redis.hash_set(
            self.keys.workspace_concurrency_counter(counter.workspace_id),
            mapping=redis_serialization.dump_model_hash(counter),
        )

    def _write_concurrency_reservation(self, reservation: ConcurrencyReservation) -> None:
        self.redis.hash_set(
            self.keys.workspace_concurrency_reservation(
                reservation.workspace_id,
                reservation.container_id,
            ),
            mapping=redis_serialization.dump_model_hash(reservation),
        )

    def _list_container_state_by_index(self, index_key: str) -> list[SchedulerContainerState]:
        states: list[SchedulerContainerState] = []
        for state_key in sorted(
            redis_serialization.redis_strings(self.redis.set_members(index_key))
        ):
            raw = self.redis.hash_get_all(state_key)
            if not raw:
                container_id = state_key.rsplit(":", 2)[-2]
                if self.redis.get(self.keys.container_exit_code(container_id)) is None:
                    self.redis.set_remove(index_key, state_key)
                continue
            states.append(redis_serialization.load_model_hash(SchedulerContainerState, raw))
        if int(self.redis.set_cardinality(index_key)) == 0:
            self.redis.delete(index_key)
        return states


@dataclass(init=False, slots=True)
class RedisOrphanedContainerConfirmationRepository:
    """When a container was first seen unscheduled, shared across schedulers.

    The confirmation window exists so a container mid-handoff is not mistaken for
    an abandoned one. Held in a scheduler's memory it is not a window at all once
    a second scheduler exists: each keeps its own clock, both start it at a
    different moment, and both independently conclude the container is dead. It
    also resets on every restart, which quietly forgives containers that really
    were orphaned.
    """

    redis: RedisClient
    keys: SchedulerStateKeys

    def __init__(self, redis: RedisClient, keys: SchedulerStateKeys | None = None) -> None:
        self.redis = redis
        self.keys = keys or SchedulerStateKeys(redis)

    def first_observed_at(
        self,
        container_id: str,
        *,
        now: datetime,
        ttl_seconds: int,
    ) -> datetime:
        """Record this observation, and answer with the earliest one recorded."""
        key = self.keys.orphaned_container_confirmation(container_id)
        self.redis.set(key, now.isoformat(), ex=max(ttl_seconds, 1), nx=True)
        stored = self.redis.get(key)
        if stored is None:
            return now
        try:
            return datetime.fromisoformat(str(stored))
        except ValueError:
            return now

    def claim_confirmed(self, container_id: str) -> bool:
        """Take the right to fail this container, exactly once across schedulers.

        The observation record doubles as the claim: whichever scheduler removes
        it is the one that acts, and the others find nothing and move on. A
        separate lock would leave the same container failable twice in the gap
        between reading the clock and taking the lock.
        """
        key = self.keys.orphaned_container_confirmation(container_id)
        return self.redis.getdel(key) is not None

    def forget(self, container_id: str) -> None:
        self.redis.delete(self.keys.orphaned_container_confirmation(container_id))


@dataclass(init=False, slots=True)
class RedisWorkerNetworkIpRepository:
    redis: RedisClient
    keys: SchedulerStateKeys

    def __init__(self, redis: RedisClient, keys: SchedulerStateKeys | None = None) -> None:
        self.redis = redis
        self.keys = keys or SchedulerStateKeys(redis)

    def list_ips(self, network_prefix: str) -> list[str]:
        return sorted(
            redis_serialization.redis_strings(
                self.redis.set_members(self.keys.network_ip_index(network_prefix))
            )
        )

    def list_assignments(self, network_prefix: str) -> list[ContainerIpAssignment]:
        assignments: list[ContainerIpAssignment] = []
        index_key = self.keys.network_ip_index(network_prefix)
        for ip_address in self.list_ips(network_prefix):
            owner = self.redis.get(self.keys.network_ip_owner(network_prefix, ip_address))
            if owner is None or redis_serialization.redis_text(owner) == "":
                self.redis.set_remove(index_key, ip_address)
                self.redis.hash_delete(self.keys.network_ip_ref_counts(network_prefix), ip_address)
                continue
            assignments.append(
                ContainerIpAssignment(
                    container_id=redis_serialization.redis_text(owner), ip_address=ip_address
                )
            )
        assignments.sort(key=lambda item: item.container_id)
        return assignments

    def get_container_ip(self, network_prefix: str, container_id: str) -> str | None:
        raw = self.redis.get(self.keys.network_container_ip(network_prefix, container_id))
        if raw is None:
            return None
        return redis_serialization.redis_text(raw)

    def list_container_network_prefixes(self, container_id: str) -> list[str]:
        prefixes = set(
            redis_serialization.redis_strings(
                self.redis.set_members(self.keys.network_container_prefixes(container_id))
            )
        )
        base = self.redis.key("worker-network") + ":"
        suffix = f":containers:{container_id}:ip"
        for key in self.redis.scan(f"{base}*{suffix}"):
            if key.startswith(base) and key.endswith(suffix):
                prefix = key[len(base) : -len(suffix)]
                if prefix:
                    prefixes.add(prefix)
        return sorted(prefixes)

    def set_container_ip(
        self,
        network_prefix: str,
        container_id: str,
        ip_address: str,
    ) -> NetworkIpMutationPlan:
        current_ip = self.get_container_ip(network_prefix, container_id) or ""
        existing_owner = self.redis.get(self.keys.network_ip_owner(network_prefix, ip_address))
        plan = plan_set_network_container_ip(
            container_id=container_id,
            requested_ip=ip_address,
            current_ip=current_ip,
            existing_owner=redis_serialization.redis_text(existing_owner)
            if existing_owner is not None
            else "",
        )
        if plan.action is NetworkIpMutationAction.Reject:
            raise SchedulerRepositoryError(plan.reason)
        if plan.previous_ip_address and plan.cleanup_previous_owner:
            self.redis.delete(self.keys.network_ip_owner(network_prefix, plan.previous_ip_address))
            self.redis.hash_delete(
                self.keys.network_ip_ref_counts(network_prefix),
                plan.previous_ip_address,
            )
            self.redis.set_remove(
                self.keys.network_ip_index(network_prefix),
                plan.previous_ip_address,
            )
        self.redis.set(
            self.keys.network_container_ip(network_prefix, container_id),
            ip_address,
        )
        self.redis.set_add(
            self.keys.network_container_prefixes(container_id),
            network_prefix,
        )
        self.redis.set_add(self.keys.network_ip_index(network_prefix), ip_address)
        self.redis.hash_set(self.keys.network_ip_ref_counts(network_prefix), ip_address, "1")
        self.redis.set(self.keys.network_ip_owner(network_prefix, ip_address), container_id)
        return plan

    def remove_container_ip(
        self,
        network_prefix: str,
        container_id: str,
    ) -> NetworkIpMutationPlan:
        current_ip = self.get_container_ip(network_prefix, container_id) or ""
        owner = ""
        if current_ip:
            raw_owner = self.redis.get(self.keys.network_ip_owner(network_prefix, current_ip))
            owner = redis_serialization.redis_text(raw_owner) if raw_owner is not None else ""
        plan = plan_remove_network_container_ip(
            container_id=container_id,
            current_ip=current_ip,
            owner=owner,
        )
        if plan.action is NetworkIpMutationAction.Noop:
            self._remove_container_network_prefix(container_id, network_prefix)
            return plan
        self.redis.delete(self.keys.network_container_ip(network_prefix, container_id))
        self._remove_container_network_prefix(container_id, network_prefix)
        if plan.cleanup_current_owner:
            self.redis.delete(self.keys.network_ip_owner(network_prefix, current_ip))
            self.redis.hash_delete(self.keys.network_ip_ref_counts(network_prefix), current_ip)
            self.redis.set_remove(self.keys.network_ip_index(network_prefix), current_ip)
        return plan

    def move_container_ip(
        self,
        network_prefix: str,
        from_container_id: str,
        to_container_id: str,
        ip_address: str,
    ) -> NetworkIpMutationPlan:
        from_ip = self.get_container_ip(network_prefix, from_container_id) or ""
        to_ip = self.get_container_ip(network_prefix, to_container_id) or ""
        raw_owner = self.redis.get(self.keys.network_ip_owner(network_prefix, ip_address))
        owner = redis_serialization.redis_text(raw_owner) if raw_owner is not None else ""
        plan = plan_move_network_container_ip(
            requested_ip=ip_address,
            source_current_ip=from_ip,
            target_current_ip=to_ip,
            owner=owner,
            source_container_id=from_container_id,
            target_container_id=to_container_id,
        )
        if plan.action is NetworkIpMutationAction.Reject:
            raise SchedulerRepositoryError(plan.reason)
        self.redis.delete(self.keys.network_container_ip(network_prefix, from_container_id))
        self._remove_container_network_prefix(from_container_id, network_prefix)
        self.redis.set(
            self.keys.network_container_ip(network_prefix, to_container_id),
            ip_address,
        )
        self.redis.set_add(
            self.keys.network_container_prefixes(to_container_id),
            network_prefix,
        )
        self.redis.set(
            self.keys.network_ip_owner(network_prefix, ip_address),
            to_container_id,
        )
        self.redis.set_add(self.keys.network_ip_index(network_prefix), ip_address)
        self.redis.hash_set(self.keys.network_ip_ref_counts(network_prefix), ip_address, "1")
        return plan

    def remove_container_ips(self, container_id: str) -> None:
        for network_prefix in self.list_container_network_prefixes(container_id):
            self.remove_container_ip(network_prefix, container_id)

    def _remove_container_network_prefix(
        self,
        container_id: str,
        network_prefix: str,
    ) -> None:
        index_key = self.keys.network_container_prefixes(container_id)
        self.redis.set_remove(index_key, network_prefix)
        if int(self.redis.set_cardinality(index_key)) == 0:
            self.redis.delete(index_key)

    def set_network_lock(
        self,
        network_prefix: str,
        *,
        ttl_seconds: int = DEFAULT_NETWORK_LOCK_TTL_SECONDS,
        retries: int = 0,
    ) -> WorkerRepositoryLockRecord:
        key = self.keys.network_lock(network_prefix)
        return _acquire_token_lock(
            self.redis,
            key,
            kind=WorkerRepositoryLockKind.Network,
            owner_id=network_prefix,
            resource_id=network_prefix,
            ttl_seconds=ttl_seconds,
            retries=retries,
        )

    def remove_network_lock(
        self,
        network_prefix: str,
        token: str,
    ) -> WorkerRepositoryLockRelease:
        key = self.keys.network_lock(network_prefix)
        return _release_token_lock(
            self.redis,
            key,
            token,
            kind=WorkerRepositoryLockKind.Network,
        )


@dataclass(init=False, slots=True)
class RedisWorkerPoolStateRepository:
    redis: RedisClient
    keys: SchedulerStateKeys

    def __init__(self, redis: RedisClient, keys: SchedulerStateKeys | None = None) -> None:
        self.redis = redis
        self.keys = keys or SchedulerStateKeys(redis)

    def set_state(
        self,
        capacity_owner_id: str,
        state: WorkerPoolStateSnapshot,
    ) -> WorkerPoolStateSnapshot:
        if not capacity_owner_id:
            raise SchedulerRepositoryError("worker pool state requires a capacity owner")
        if state.capacity_owner_id != capacity_owner_id:
            raise SchedulerRepositoryError(
                "worker pool state capacity owner does not match its repository key"
            )
        self.redis.hash_set(
            self.keys.worker_pool_state(capacity_owner_id),
            mapping=redis_serialization.dump_model_hash(state),
        )
        return state

    def get_state(self, capacity_owner_id: str) -> WorkerPoolStateSnapshot:
        raw = self.redis.hash_get_all(self.keys.worker_pool_state(capacity_owner_id))
        if not raw:
            raise WorkerPoolStateNotFoundError(capacity_owner_id)
        return redis_serialization.load_model_hash(WorkerPoolStateSnapshot, raw)

    def delete_unit_state(self, capacity_owner_id: str) -> bool:
        return bool(
            self.redis.delete(
                self.keys.worker_pool_state(capacity_owner_id),
                self.keys.worker_pool_replicas(capacity_owner_id),
            )
        )


def capacity_memory_mib(memory_mib: int) -> int:
    if memory_mib <= 0:
        return memory_mib
    return (memory_mib * 125 + 99) // 100


def plan_worker_capacity_change(
    worker: SchedulerWorkerRecord,
    request: SchedulerWorkerRequest,
    change: WorkerCapacityChange,
    *,
    reserved_capacity: WorkerReservedCapacity | None = None,
) -> WorkerCapacityPlan:
    cpu_millicores = (
        reserved_capacity.cpu_millicores
        if reserved_capacity is not None
        else request.cpu_millicores
    )
    memory_mib = (
        reserved_capacity.memory_mib
        if reserved_capacity is not None
        else capacity_memory_mib(request.memory_mib)
    )
    gpu_count = (
        reserved_capacity.gpu_count
        if reserved_capacity is not None
        else gpu_count_for_capacity(request.gpu, request.gpu_count)
    )
    if change is WorkerCapacityChange.Add:
        updated = _restored_worker_capacity(
            worker,
            cpu_millicores=cpu_millicores,
            memory_mib=memory_mib,
            gpu_count=gpu_count,
        )
        return WorkerCapacityPlan(
            worker=updated,
            change=change,
            request=request,
            accepted=True,
        )

    if request.region is not None and worker.region != request.region:
        return WorkerCapacityPlan(
            worker=worker,
            change=change,
            request=request,
            accepted=False,
            reason="worker is outside the selected region",
        )
    if request.availability_zone and worker.availability_zone != request.availability_zone:
        return WorkerCapacityPlan(
            worker=worker,
            change=change,
            request=request,
            accepted=False,
            reason="worker is outside the selected availability zone",
        )
    if not request.preemptible and worker.preemptible:
        return WorkerCapacityPlan(
            worker=worker,
            change=change,
            request=request,
            accepted=False,
            reason="worker is preemptible but request is not",
        )
    if (
        gpu_count == 0
        and worker.total_gpu_count > 0
        and (
            not request.backfill
            or not request.preemptible
            or worker.free_gpu_count >= worker.total_gpu_count
        )
    ):
        return WorkerCapacityPlan(
            worker=worker,
            change=change,
            request=request,
            accepted=False,
            reason="CPU work requires eligible GPU backfill placement",
        )

    if (
        worker.free_cpu_millicores < cpu_millicores
        or worker.free_memory_mib < memory_mib
        or worker.free_gpu_count < gpu_count
    ):
        return WorkerCapacityPlan(
            worker=worker,
            change=change,
            request=request,
            accepted=False,
            reason="worker out of cpu, memory, or gpu capacity",
        )
    updated = worker.model_copy(
        update={
            "free_cpu_millicores": worker.free_cpu_millicores - cpu_millicores,
            "free_memory_mib": worker.free_memory_mib - memory_mib,
            "free_gpu_count": worker.free_gpu_count - gpu_count,
            "resource_version": worker.resource_version + 1,
            "updated_at": utc_now(),
        }
    )
    return WorkerCapacityPlan(worker=updated, change=change, request=request, accepted=True)


def _restored_worker_capacity(
    worker: SchedulerWorkerRecord,
    *,
    cpu_millicores: int,
    memory_mib: int,
    gpu_count: int,
) -> SchedulerWorkerRecord:
    return worker.model_copy(
        update={
            "free_cpu_millicores": _cap_capacity(
                worker.free_cpu_millicores + cpu_millicores, worker.total_cpu_millicores
            ),
            "free_memory_mib": _cap_capacity(
                worker.free_memory_mib + memory_mib, worker.total_memory_mib
            ),
            "free_gpu_count": _cap_capacity(
                worker.free_gpu_count + gpu_count, worker.total_gpu_count
            ),
            "resource_version": worker.resource_version + 1,
            "updated_at": utc_now(),
        }
    )


def _worker_capacity_changed(
    worker: SchedulerWorkerRecord,
    reconciled: SchedulerWorkerRecord,
) -> bool:
    return (
        worker.free_cpu_millicores != reconciled.free_cpu_millicores
        or worker.free_memory_mib != reconciled.free_memory_mib
        or worker.free_gpu_count != reconciled.free_gpu_count
    )


def reserve_concurrency(
    *,
    counter: ConcurrencyCounter,
    existing_reservation: ConcurrencyReservation | None,
    workspace_id: str,
    container_id: str,
    workspace_gpu_quota: int,
    workspace_cpu_quota_millicores: int,
    request_gpu_count: int,
    request_cpu_millicores: int,
    now: datetime | None = None,
) -> ConcurrencyReservationDecision:
    if existing_reservation is not None:
        return ConcurrencyReservationDecision(
            status=ConcurrencyReservationStatus.Ok,
            counter=counter,
            reservation=existing_reservation,
            changed=False,
        )
    if not counter.initialized or counter.repairing:
        return ConcurrencyReservationDecision(
            status=ConcurrencyReservationStatus.Repairing,
            counter=counter,
            reason="concurrency counter is not initialized",
        )
    if counter.gpu_count + request_gpu_count > workspace_gpu_quota:
        return ConcurrencyReservationDecision(
            status=ConcurrencyReservationStatus.GpuExceeded,
            counter=counter,
            reason="gpu quota exceeded",
        )
    if counter.cpu_millicores + request_cpu_millicores > workspace_cpu_quota_millicores:
        return ConcurrencyReservationDecision(
            status=ConcurrencyReservationStatus.CpuExceeded,
            counter=counter,
            reason="cpu quota exceeded",
        )
    current_time = now or utc_now()
    updated = counter.model_copy(
        update={
            "gpu_count": counter.gpu_count + request_gpu_count,
            "cpu_millicores": counter.cpu_millicores + request_cpu_millicores,
            "initialized": True,
            "repairing": False,
            "updated_at": current_time,
        }
    )
    reservation = ConcurrencyReservation(
        workspace_id=workspace_id,
        container_id=container_id,
        gpu_count=request_gpu_count,
        cpu_millicores=request_cpu_millicores,
        created_at=current_time,
    )
    return ConcurrencyReservationDecision(
        status=ConcurrencyReservationStatus.Ok,
        counter=updated,
        reservation=reservation,
        changed=True,
    )


def release_concurrency(
    *,
    counter: ConcurrencyCounter,
    reservation: ConcurrencyReservation | None,
    now: datetime | None = None,
) -> ConcurrencyReservationDecision:
    if reservation is None:
        return ConcurrencyReservationDecision(
            status=ConcurrencyReservationStatus.Missing,
            counter=counter,
            changed=False,
        )
    if not counter.initialized or counter.repairing:
        return ConcurrencyReservationDecision(
            status=ConcurrencyReservationStatus.Repairing,
            counter=counter,
            reservation=reservation,
            reason="concurrency counter is not initialized",
        )
    updated = counter.model_copy(
        update={
            "gpu_count": max(counter.gpu_count - reservation.gpu_count, 0),
            "cpu_millicores": max(counter.cpu_millicores - reservation.cpu_millicores, 0),
            "updated_at": now or utc_now(),
        }
    )
    return ConcurrencyReservationDecision(
        status=ConcurrencyReservationStatus.Ok,
        counter=updated,
        reservation=reservation,
        changed=True,
    )


def _concurrency_counter_ready(counter: ConcurrencyCounter) -> bool:
    return counter.initialized and not counter.repairing


def _concurrency_counter_needs_repair(
    counter: ConcurrencyCounter,
    *,
    now: datetime,
) -> bool:
    if not _concurrency_counter_ready(counter):
        return True
    if counter.repaired_at is None:
        return True
    return (
        now - counter.repaired_at
    ).total_seconds() >= DEFAULT_CONCURRENCY_COUNTER_REPAIR_INTERVAL_SECONDS


def _script_status_changed(result: list[RedisWireScalar]) -> tuple[str, bool]:
    if not result:
        msg = "redis script returned an empty result"
        raise SchedulerRepositoryError(msg)

    status = redis_serialization.redis_text(result[0])
    changed = False
    if len(result) > 1:
        changed_value = redis_serialization.redis_text(result[1])
        changed = changed_value in {"1", "true", "True"}
        return status, changed
    return status, changed


def _redis_script_text_items(result: list[RedisWireScalar]) -> list[str]:
    return [redis_serialization.redis_text(item) for item in result]


def _concurrency_decision_reason(status: ConcurrencyReservationStatus) -> str:
    if status is ConcurrencyReservationStatus.GpuExceeded:
        return "gpu quota exceeded"
    if status is ConcurrencyReservationStatus.CpuExceeded:
        return "cpu quota exceeded"
    if status is ConcurrencyReservationStatus.Repairing:
        return "concurrency counter is not initialized"
    return ""


def plan_set_network_container_ip(
    *,
    container_id: str,
    requested_ip: str,
    current_ip: str = "",
    existing_owner: str = "",
) -> NetworkIpMutationPlan:
    if existing_owner and existing_owner != container_id:
        return NetworkIpMutationPlan(
            action=NetworkIpMutationAction.Reject,
            container_id=container_id,
            ip_address=requested_ip,
            previous_ip_address=current_ip,
            owner_container_id=existing_owner,
            reason=f"ip address already reserved by {existing_owner}",
        )
    return NetworkIpMutationPlan(
        action=NetworkIpMutationAction.Set,
        container_id=container_id,
        ip_address=requested_ip,
        previous_ip_address=current_ip,
        owner_container_id=container_id,
        changed=current_ip != requested_ip,
        cleanup_previous_owner=bool(current_ip and current_ip != requested_ip),
    )


def plan_remove_network_container_ip(
    *,
    container_id: str,
    current_ip: str = "",
    owner: str = "",
) -> NetworkIpMutationPlan:
    if current_ip == "":
        return NetworkIpMutationPlan(
            action=NetworkIpMutationAction.Noop,
            container_id=container_id,
            reason="container has no assigned ip",
        )
    cleanup_owner = owner in {"", container_id}
    return NetworkIpMutationPlan(
        action=NetworkIpMutationAction.Remove,
        container_id=container_id,
        ip_address=current_ip,
        owner_container_id=owner,
        changed=True,
        cleanup_current_owner=cleanup_owner,
        reason="" if cleanup_owner else "ip owner belongs to a different container",
    )


def plan_move_network_container_ip(
    *,
    requested_ip: str,
    source_current_ip: str,
    target_current_ip: str,
    owner: str,
    source_container_id: str,
    target_container_id: str,
) -> NetworkIpMutationPlan:
    if source_current_ip != requested_ip:
        return NetworkIpMutationPlan(
            action=NetworkIpMutationAction.Reject,
            container_id=source_container_id,
            source_container_id=source_container_id,
            target_container_id=target_container_id,
            ip_address=requested_ip,
            previous_ip_address=source_current_ip,
            reason="source container does not own requested ip",
        )
    if owner != source_container_id:
        return NetworkIpMutationPlan(
            action=NetworkIpMutationAction.Reject,
            container_id=source_container_id,
            source_container_id=source_container_id,
            target_container_id=target_container_id,
            ip_address=requested_ip,
            owner_container_id=owner,
            reason="ip owner mismatch",
        )
    if target_current_ip and target_current_ip != requested_ip:
        return NetworkIpMutationPlan(
            action=NetworkIpMutationAction.Reject,
            container_id=target_container_id,
            source_container_id=source_container_id,
            target_container_id=target_container_id,
            ip_address=requested_ip,
            previous_ip_address=target_current_ip,
            owner_container_id=owner,
            reason="destination container already has a different ip",
        )
    return NetworkIpMutationPlan(
        action=NetworkIpMutationAction.Move,
        container_id=target_container_id,
        source_container_id=source_container_id,
        target_container_id=target_container_id,
        ip_address=requested_ip,
        previous_ip_address=source_current_ip,
        owner_container_id=target_container_id,
        changed=source_container_id != target_container_id,
    )


def _route_for_container(
    route: AgentBackendRoute | None,
    *,
    container_id: str,
) -> AgentBackendRoute | None:
    if route is None:
        return None
    if route.container_id == container_id:
        return route
    return route.model_copy(update={"container_id": route.container_id or container_id})


def _same_route(
    left: AgentBackendRoute | None,
    right: AgentBackendRoute | None,
) -> bool:
    if left is None or right is None:
        return False
    return left.route_id == right.route_id


def capacity_owner_key_segment(capacity_owner_id: str) -> str:
    return sha256(capacity_owner_id.encode("utf-8")).hexdigest()


def _acquire_token_lock(
    redis: RedisClient,
    key: str,
    *,
    kind: WorkerRepositoryLockKind,
    owner_id: str,
    resource_id: str,
    ttl_seconds: int,
    retries: int,
) -> WorkerRepositoryLockRecord:
    token = token_urlsafe(24)
    attempts = max(retries, 0) + 1
    for attempt in range(attempts):
        if try_acquire_token_lock(redis, key, token, ttl_seconds=ttl_seconds):
            return _lock_record(
                kind,
                key,
                token=token,
                owner_id=owner_id,
                resource_id=resource_id,
                ttl_seconds=ttl_seconds,
                retries=retries,
            )
        if attempt + 1 < attempts:
            time.sleep(0.01)
    return _lock_record(
        kind,
        key,
        token=None,
        owner_id=owner_id,
        resource_id=resource_id,
        ttl_seconds=ttl_seconds,
        retries=retries,
    )


async def _acquire_token_lock_async(
    redis: AsyncRedisClient,
    key: str,
    *,
    kind: WorkerRepositoryLockKind,
    owner_id: str,
    resource_id: str,
    ttl_seconds: int,
    retries: int,
) -> WorkerRepositoryLockRecord:
    token = token_urlsafe(24)
    attempts = max(retries, 0) + 1
    for attempt in range(attempts):
        if await try_acquire_token_lock_async(redis, key, token, ttl_seconds=ttl_seconds):
            return _lock_record(
                kind,
                key,
                token=token,
                owner_id=owner_id,
                resource_id=resource_id,
                ttl_seconds=ttl_seconds,
                retries=retries,
            )
        if attempt + 1 < attempts:
            await asyncio.sleep(0.01)
    return _lock_record(
        kind,
        key,
        token=None,
        owner_id=owner_id,
        resource_id=resource_id,
        ttl_seconds=ttl_seconds,
        retries=retries,
    )


def _lock_record(
    kind: WorkerRepositoryLockKind,
    key: str,
    *,
    token: str | None,
    owner_id: str,
    resource_id: str,
    ttl_seconds: int,
    retries: int,
) -> WorkerRepositoryLockRecord:
    return WorkerRepositoryLockRecord(
        kind=kind,
        key=key,
        token=token or "",
        owner_id=owner_id,
        resource_id=resource_id,
        ttl_seconds=ttl_seconds,
        retries=retries,
        acquired=token is not None,
    )


def _worker_with_status(
    worker: SchedulerWorkerRecord,
    status: SchedulerWorkerStatus,
    *,
    now: datetime | None,
    unavailable_reason: WorkerUnavailableReason | None,
    unavailable_detail: str,
) -> SchedulerWorkerRecord:
    return worker.model_copy(
        update={
            "status": status,
            "unavailable_reason": unavailable_reason,
            "unavailable_detail": unavailable_detail,
            "resource_version": worker.resource_version + 1,
            "updated_at": now or utc_now(),
        }
    )


def _release_token_lock(
    redis: RedisClient,
    key: str,
    token: str,
    *,
    kind: WorkerRepositoryLockKind,
) -> WorkerRepositoryLockRelease:
    return _token_lock_release(kind, key, token, release_token_lock(redis, key, token))


async def _release_token_lock_async(
    redis: AsyncRedisClient,
    key: str,
    token: str,
    *,
    kind: WorkerRepositoryLockKind,
) -> WorkerRepositoryLockRelease:
    status = await release_token_lock_async(redis, key, token)
    return _token_lock_release(kind, key, token, status)


def _token_lock_release(
    kind: WorkerRepositoryLockKind,
    key: str,
    token: str,
    status: TokenLockReleaseStatus,
) -> WorkerRepositoryLockRelease:
    if status is TokenLockReleaseStatus.Missing:
        return WorkerRepositoryLockRelease(
            kind=kind,
            key=key,
            token=token,
            released=False,
            reason="lock not found",
        )
    if status is TokenLockReleaseStatus.TokenMismatch:
        return WorkerRepositoryLockRelease(
            kind=kind,
            key=key,
            token=token,
            released=False,
            reason="lock token mismatch",
        )
    return WorkerRepositoryLockRelease(kind=kind, key=key, token=token, released=True)


def _cap_capacity(value: int, total: int) -> int:
    if total > 0:
        return min(value, total)
    return value


@dataclass(init=False, slots=True)
class AsyncRedisSchedulerContainerReader:
    redis: AsyncRedisClient
    keys: SchedulerStateKeys

    def __init__(
        self,
        redis: AsyncRedisClient,
        keys: SchedulerStateKeys | None = None,
    ) -> None:
        self.redis = redis
        self.keys = keys or SchedulerStateKeys(redis)

    async def get_container_state(self, container_id: str) -> SchedulerContainerState | None:
        raw = await self.redis.hash_get_all(self.keys.container_state(container_id))
        if not raw:
            return None
        return redis_serialization.load_model_hash(SchedulerContainerState, raw)

    async def list_by_stub(self, stub_id: str) -> list[SchedulerContainerState]:
        state_keys = sorted(
            redis_serialization.redis_strings(
                await self.redis.set_members(self.keys.container_stub_index(stub_id))
            )
        )
        if not state_keys:
            return []
        raw_states = await asyncio.gather(
            *(self.redis.hash_get_all(state_key) for state_key in state_keys)
        )
        return [
            redis_serialization.load_model_hash(SchedulerContainerState, raw)
            for raw in raw_states
            if raw
        ]

    async def get_container_address_map(
        self,
        container_id: str,
    ) -> SchedulerContainerAddressMap:
        maps = await self.get_container_address_maps([container_id])
        return maps[container_id]

    async def get_container_address(
        self,
        container_id: str,
    ) -> SchedulerContainerAddress | None:
        raw = await self.redis.get(self.keys.container_address(container_id))
        if raw is None:
            return None
        return redis_serialization.load_model_json(SchedulerContainerAddress, raw)

    async def get_container_address_maps(
        self,
        container_ids: Sequence[str],
    ) -> dict[str, SchedulerContainerAddressMap]:
        raw_maps = await self.redis.mget(
            [self.keys.container_address_map(container_id) for container_id in container_ids]
        )
        return {
            container_id: (
                redis_serialization.load_model_json(SchedulerContainerAddressMap, raw)
                if raw is not None
                else SchedulerContainerAddressMap(container_id=container_id)
            )
            for container_id, raw in zip(container_ids, raw_maps, strict=True)
        }

    async def agent_route_ids(self, container_id: str) -> set[str]:
        raw_address, raw_address_map, raw_worker_address = await self.redis.mget(
            [
                self.keys.container_address(container_id),
                self.keys.container_address_map(container_id),
                self.keys.worker_address(container_id),
            ]
        )
        routes: list[AgentBackendRoute] = []
        if raw_address is not None:
            address = redis_serialization.load_model_json(SchedulerContainerAddress, raw_address)
            if address.route is not None:
                routes.append(address.route)
        if raw_address_map is not None:
            address_map = redis_serialization.load_model_json(
                SchedulerContainerAddressMap,
                raw_address_map,
            )
            routes.extend(address_map.routes)
        if raw_worker_address is not None:
            worker_address = redis_serialization.load_model_json(
                SchedulerContainerAddress,
                raw_worker_address,
            )
            if worker_address.route is not None:
                routes.append(worker_address.route)
        return {route.route_id for route in routes if route.route_id}
