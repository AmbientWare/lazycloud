from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import JsonValue


def _connection(*, phase: str = "ready") -> dict[str, JsonValue]:
    return {
        "id": "11111111-1111-4111-8111-111111111111",
        "account_id": "123456789012",
        "phase": phase,
        "revision": 1,
        "accepts_placement": phase == "ready",
        "can_manage_existing_capacity": phase == "ready",
        "available_actions": ["reconnect", "remove"] if phase == "ready" else [],
        "detail": "AWS compute is available for this workspace.",
        "customer_action": None,
        "next_retry_at": None,
        "active_authorization": None,
        "pending_authorization": None,
        "retiring_authorization": None,
        "created_at": "2026-07-15T12:00:00Z",
        "updated_at": "2026-07-15T12:00:01Z",
    }


def _policy() -> dict[str, JsonValue]:
    return {
        "revision": 1,
        "default_placement": "managed",
        "aws": {
            "default_region": "us-east-1",
            "max_cpu_instances": 10,
            "max_gpu_instances": 2,
            "allowed_regions": ["us-east-1"],
            "allowed_instance_types": [],
            "idle_timeout_seconds": 300,
            "root_volume_gib": 200,
        },
        "created_at": "2026-07-15T12:00:00Z",
        "updated_at": "2026-07-15T12:00:01Z",
    }


@dataclass(slots=True)
class _ComputeChannel:
    calls: list[tuple[str, str, dict[str, JsonValue] | None]] = field(default_factory=list)

    def get(self, path: str) -> JsonValue:
        self.calls.append(("GET", path, None))
        if "/aws-connection?" in path:
            return {"connection": _connection()}
        if "/policy?" in path:
            return _policy()
        if "/catalog?" in path:
            return {"data": [], "next": ""}
        if "/summary?" in path:
            return {
                "policy": _policy(),
                "connection": {"account_id": "123456789012", "phase": "ready"},
                "instances": {"total": 0, "ready": 0, "pending": 0, "degraded": 0},
                "cost": {
                    "hourly_micros": 0,
                    "daily_micros": 0,
                    "currency": "USD",
                    "estimated": True,
                },
                "workload_count": 0,
            }
        if "/instances?" in path or "/workloads?" in path:
            return {"data": [], "next": ""}
        if "/pools/fast-pool/offers?" in path:
            return {
                "data": [
                    {
                        "id": "cpu-large",
                        "provider": "provider-a",
                        "instance_type": "cpu-large",
                        "region": "lab",
                    }
                ],
                "next": "",
            }
        if "/pools/fast-pool/machines?" in path:
            return {"data": [], "next": ""}
        raise AssertionError(f"unexpected GET {path}")

    def post(
        self,
        path: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue:
        self.calls.append(("POST", path, payload))
        if path.endswith("/pools/fast-pool/capacity?workspace=team+one"):
            return {"name": "fast-pool", "reserved_nodes": 2, "max_spend_micros": 3_000_000}
        if path.endswith("/pools/fast-pool/join-token?workspace=team+one"):
            return {"token": "join-token", "expires_at": "2026-07-20T12:00:00Z"}
        if path.endswith("/pools/fast-pool/join-command?workspace=team+one"):
            return {
                "command": "agent join --token join-token",
                "expires_at": "2026-07-20T12:00:00Z",
            }
        if path.endswith("/validate?workspace=team+one"):
            return _connection()
        if path.endswith("/retry?workspace=team+one"):
            return _connection(phase="revoking")
        if path.endswith("/reconnect?workspace=team+one"):
            return {
                "connection": _connection(phase="reconnect_pending"),
                "authorization": {"url": None, "external_id": "x" * 32},
            }
        return {
            "connection": _connection(phase="awaiting_authorization"),
            "authorization": {"url": "https://console.aws.amazon.com/", "external_id": None},
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue:
        self.calls.append((method, path, payload))
        if method == "DELETE" and path.endswith("/aws-connection?workspace=team+one"):
            return {"connection": _connection(phase="disconnect_draining")}
        if method == "DELETE" and path.endswith("/reconnect?workspace=team+one"):
            return _connection()
        if method == "DELETE" and path.endswith("/pools/fast-pool/join-token?workspace=team+one"):
            return None
        if method == "PATCH" and path.endswith("/pools/fast-pool/capacity?workspace=team+one"):
            return {"name": "fast-pool", "reserved_nodes": 2, "max_spend_micros": 4_000_000}
        return _policy()
